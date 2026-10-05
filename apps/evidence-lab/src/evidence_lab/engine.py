"""Run one durable query against frozen evidence with a bounded release gate."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from evidence_lab.domain import CallContext, ProviderError
from evidence_lab.policy import evaluate_checks, policy_state, structural_check
from evidence_lab.retrieval import retrieve_evidence

INSUFFICIENT = "I couldn't find supporting evidence for an answer in this corpus."
UNSUPPORTED = "I couldn't produce an answer supported by the retrieved sources."


class QueryEngine:
    def __init__(self, store, hub, config):
        self.store, self.hub, self.config = store, hub, config

    async def run(self, job: dict) -> dict:
        run_id = job["payload"]["run_id"]
        run = self.store.get_run(run_id)
        question = run["question"]
        corpus_id = run["corpus_id"]
        started = time.monotonic()
        remaining = float(self.config.runtime.query_deadline_seconds)
        created_at = run.get("created_at")
        if created_at:
            when = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            remaining -= max(0, (datetime.now(timezone.utc) - when).total_seconds())
        ctx = CallContext.for_seconds(run_id, "queries", max(0, remaining),
                                     self.config.runtime.max_remote_attempts_per_query)
        ctx.attempts_used = len(self.store.get_calls(run_id))
        timings = {}
        evidence = None
        stage = "retrieving"

        def update(**fields):
            self.store.update_run(run_id, fields, lease=job)

        def event(kind, **fields):
            self.store.append_event(run_id, {"type": kind, **fields}, lease=job)

        def terminal(status, *, answer=None, blocks=None, checks=None, error=None, code=None, message=None):
            timings["worker_total_seconds"] = time.monotonic() - started
            timings["queue_seconds"] = max(0, self.config.runtime.query_deadline_seconds - remaining)
            timings["total_seconds"] = timings["queue_seconds"] + timings["worker_total_seconds"]
            state = policy_state(self.config)
            update(status=status, answer=answer, blocks=blocks or [], checks=checks or [],
                   evidence=evidence.model_dump(mode="json") if evidence else None,
                   qualification="fixture_only" if self.config.runtime.mode == "mock" else state["state"],
                   error=error, code=code, message=message, timings=timings)
            return {"run_id": run_id, "status": status, "code": code}

        try:
            ctx.remaining()
            state = policy_state(self.config)
            if self.config.verification.mode == "gated" and self.config.runtime.mode == "live" and not state["release_allowed"]:
                raise ProviderError("policy_not_ready", state["reason"])
            update(status=stage, config_fingerprint=self.config.fingerprint(), mode=self.config.runtime.mode)
            event("started", mode=self.config.runtime.mode, policy=state)
            stamp = time.monotonic()
            async with asyncio.timeout(ctx.remaining()):
                evidence = await retrieve_evidence(question, corpus_id, self.store, self.hub, self.config, ctx)
            timings["retrieval_seconds"] = time.monotonic() - stamp
            update(evidence=evidence.model_dump(mode="json"))
            event("evidence", evidence_hash=evidence.content_hash, evidence=evidence.model_dump(mode="json"))
            if not evidence.items:
                return terminal("abstained", message=INSUFFICIENT, code="insufficient_evidence")
            stage = "generating"
            update(status=stage)
            stamp = time.monotonic()
            async with asyncio.timeout(ctx.remaining()):
                draft = await self.hub.generate(question, evidence, ctx)
            timings["generation_seconds"] = time.monotonic() - stamp
            maximum_repairs = self.config.verification.max_content_repairs
            for round_number in range(maximum_repairs + 1):
                round_id = "initial" if round_number == 0 else "repair"
                event("draft", round_id=round_id, draft=draft.model_dump(mode="json"), answer_hash=draft.content_hash)
                structure = structural_check(draft, evidence)
                if not structure["accepted"]:
                    event("structural_rejection", **structure)
                    raise ProviderError("invalid_response", "Draft citations or quotation checks failed")
                stage = "verifying"
                update(status=stage)
                stamp = time.monotonic()
                async with asyncio.timeout(ctx.remaining()):
                    verification = await self.hub.verify(question, draft, evidence, ctx, round_id=round_id)
                timings[f"verification_{round_id}_seconds"] = time.monotonic() - stamp
                verdict = evaluate_checks(draft, evidence, verification, self.config.verification.score_threshold,
                                          expected_round_id=round_id)
                event("verification", round_id=round_id, result=verification.model_dump(mode="json"), gate=verdict)
                if verdict["technical_failures"]:
                    code = verification.execution_status if verification.execution_status != "ok" else "incomplete_coverage"
                    raise ProviderError(code, "Verification did not complete the required checks")
                if verdict["accepted"]:
                    ctx.remaining()
                    state = policy_state(self.config)
                    if not state["release_allowed"]:
                        return terminal("shadow", checks=verdict["checks"], code="policy_not_qualified",
                                        message="The candidate draft was checked but is only available in the unverified operator trace.")
                    return terminal("answered", answer=draft.render(), blocks=[b.model_dump(mode="json") for b in draft.blocks],
                                    checks=verdict["checks"])
                if round_number >= maximum_repairs:
                    return terminal("abstained", checks=verdict["checks"], message=UNSUPPORTED, code="unsupported_after_verification")
                stage = "repairing"
                update(status=stage)
                stamp = time.monotonic()
                async with asyncio.timeout(ctx.remaining()):
                    draft = await self.hub.generate(question, evidence, ctx, repair={
                        "failed_checks": verdict["failed_check_ids"],
                        "original_draft": draft.model_dump(mode="json")})
                timings["repair_generation_seconds"] = time.monotonic() - stamp
            raise RuntimeError("Unreachable gate state")
        except (TimeoutError, asyncio.TimeoutError):
            return terminal("timed_out", error="The query deadline was exceeded.", code="timeout")
        except ProviderError as exc:
            if exc.status == "lease_lost":
                raise
            status = "timed_out" if exc.status == "timeout" else (
                "cancelled" if exc.status == "cancelled" else "verification_unavailable" if stage == "verifying" else "failed")
            return terminal(status, error=str(exc), code=exc.status)
