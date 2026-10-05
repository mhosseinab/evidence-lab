"""Run one durable query against frozen evidence with a bounded release gate."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from langsmith import tracing_context

from evidence_lab.domain import CallContext, Draft, ProviderError
from evidence_lab.graph import compile_query_graph
from evidence_lab.integrations.models import LedgerChatModel, verifier_runnable
from evidence_lab.integrations.tools import retrieval_tool
from evidence_lab.langsmith_trace import export_trace
from evidence_lab.memory import question_with_context
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
        question = question_with_context(run)
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
        graph_steps = []
        completed_result = None
        evidence = None
        stage = "retrieving"

        def update(**fields):
            self.store.update_run(run_id, fields, lease=job)

        def event(kind, **fields):
            self.store.append_event(run_id, {"type": kind, **fields}, lease=job)

        def terminal(status, *, answer=None, blocks=None, checks=None, error=None, code=None, message=None):
            nonlocal completed_result
            timings["worker_total_seconds"] = time.monotonic() - started
            timings["queue_seconds"] = max(0, self.config.runtime.query_deadline_seconds - remaining)
            timings["total_seconds"] = timings["queue_seconds"] + timings["worker_total_seconds"]
            state = policy_state(self.config)
            update(status=status, answer=answer, blocks=blocks or [], checks=checks or [],
                   evidence=evidence.model_dump(mode="json") if evidence else None,
                   qualification="fixture_only" if self.config.runtime.mode == "mock" else state["state"],
                   error=error, code=code, message=message, timings=timings, graph_steps=list(graph_steps))
            completed_result = {"run_id": run_id, "status": status, "code": code}
            return completed_result

        async def retrieve(state):
            nonlocal evidence
            stamp = time.monotonic()
            tool = retrieval_tool(corpus_id=corpus_id, store=self.store, hub=self.hub,
                                  config=self.config, context=ctx, retrieve=retrieve_evidence)
            async with asyncio.timeout(ctx.remaining()):
                evidence = await tool.ainvoke({"question": question})
            timings["retrieval_seconds"] = time.monotonic() - stamp
            update(evidence=evidence.model_dump(mode="json"))
            event("evidence", evidence_hash=evidence.content_hash, evidence=evidence.model_dump(mode="json"))
            if not evidence.items:
                return {"evidence": evidence, "terminal_fields": {
                    "status": "abstained", "message": INSUFFICIENT, "code": "insufficient_evidence"}}
            return {"evidence": evidence}

        async def generate(state):
            nonlocal stage
            stage = "generating"
            update(status=stage)
            stamp = time.monotonic()
            model = LedgerChatModel(hub=self.hub, evidence=state["evidence"], context=ctx)
            async with asyncio.timeout(ctx.remaining()):
                draft = await model.with_structured_output(Draft).ainvoke(question)
            timings["generation_seconds"] = time.monotonic() - stamp
            return {"draft": draft, "round_number": 0}

        async def structural(state):
            draft, pack = state["draft"], state["evidence"]
            round_id = "initial" if state["round_number"] == 0 else "repair"
            event("draft", round_id=round_id, draft=draft.model_dump(mode="json"), answer_hash=draft.content_hash)
            structure = structural_check(draft, pack)
            if not structure["accepted"]:
                event("structural_rejection", **structure)
                raise ProviderError("invalid_response", "Draft citations or quotation checks failed")
            return {}

        async def verify(state):
            nonlocal stage
            stage = "verifying"
            update(status=stage)
            round_id = "initial" if state["round_number"] == 0 else "repair"
            stamp = time.monotonic()
            verifier = verifier_runnable(self.hub, state["evidence"], ctx, round_id)
            async with asyncio.timeout(ctx.remaining()):
                verification = await verifier.ainvoke({"question": question, "draft": state["draft"]})
            timings[f"verification_{round_id}_seconds"] = time.monotonic() - stamp
            verdict = evaluate_checks(state["draft"], state["evidence"], verification,
                                      self.config.verification.score_threshold, expected_round_id=round_id)
            event("verification", round_id=round_id, result=verification.model_dump(mode="json"), gate=verdict)
            if verdict["technical_failures"]:
                code = verification.execution_status if verification.execution_status != "ok" else "incomplete_coverage"
                raise ProviderError(code, "Verification did not complete the required checks")
            if not verdict["accepted"] and state["round_number"] >= self.config.verification.max_content_repairs:
                return {"verdict": verdict, "terminal_fields": {"status": "abstained", "checks": verdict["checks"],
                        "message": UNSUPPORTED, "code": "unsupported_after_verification"}}
            return {"verdict": verdict}

        async def repair(state):
            nonlocal stage
            stage = "repairing"
            update(status=stage)
            stamp = time.monotonic()
            model = LedgerChatModel(hub=self.hub, evidence=state["evidence"], context=ctx, repair={
                "failed_checks": state["verdict"]["failed_check_ids"],
                "original_draft": state["draft"].model_dump(mode="json")})
            async with asyncio.timeout(ctx.remaining()):
                draft = await model.with_structured_output(Draft).ainvoke(question)
            timings["repair_generation_seconds"] = time.monotonic() - stamp
            return {"draft": draft, "round_number": state["round_number"] + 1}

        async def release(state):
            ctx.remaining()
            release_policy = policy_state(self.config)
            if not release_policy["release_allowed"]:
                return {"terminal_fields": {"status": "shadow", "checks": state["verdict"]["checks"],
                        "code": "policy_not_qualified",
                        "message": "The candidate draft was checked but is only available in the unverified operator trace."}}
            return {"terminal_fields": {"status": "answered", "answer": state["draft"].render(),
                    "blocks": [block.model_dump(mode="json") for block in state["draft"].blocks],
                    "checks": state["verdict"]["checks"]}}

        async def finish(state):
            # Release metadata writes can block after the earlier policy check.
            # Recheck at the final publication boundary before exposing any answer.
            if state["terminal_fields"]["status"] == "answered":
                ctx.remaining()
            # Include the release/publication boundary in the same fenced result write.
            graph_steps.append({"node": "terminal", "status": "completed", "elapsed_seconds": 0.0})
            return {"result": terminal(**state["terminal_fields"])}

        def observe(name, node):
            async def observed(state):
                # Publication is the last fenced write; never append events afterward.
                if name == "terminal":
                    return await node(state)
                stamp = time.monotonic()
                event("graph_node", node=name, status="started")
                try:
                    result = await node(state)
                except Exception:
                    step = {"node": name, "status": "failed", "elapsed_seconds": time.monotonic() - stamp}
                    graph_steps.append(step)
                    event("graph_node", **step)
                    raise
                step = {"node": name, "status": "completed", "elapsed_seconds": time.monotonic() - stamp}
                event("graph_node", **step)
                graph_steps.append(step)
                update(graph_steps=list(graph_steps))
                return result
            return observed

        try:
            ctx.remaining()
            policy = policy_state(self.config)
            if self.config.verification.mode == "gated" and self.config.runtime.mode == "live" and not policy["release_allowed"]:
                raise ProviderError("policy_not_ready", policy["reason"])
            update(status=stage, config_fingerprint=self.config.fingerprint(), mode=self.config.runtime.mode)
            event("started", mode=self.config.runtime.mode, policy=policy)
            nodes = {"retrieve": retrieve, "generate": generate, "structural": structural,
                     "verify": verify, "repair": repair, "release": release, "terminal": finish}
            graph = compile_query_graph({name: observe(name, node) for name, node in nodes.items()})
            # Ambient tracing settings must never export graph state or document text.
            with tracing_context(enabled=False):
                final_state = await graph.ainvoke({}, config={"recursion_limit": 20})
            return final_state["result"]
        except (TimeoutError, asyncio.TimeoutError):
            return terminal("timed_out", error="The query deadline was exceeded.", code="timeout")
        except ProviderError as exc:
            if exc.status == "lease_lost":
                raise
            status = "timed_out" if exc.status == "timeout" else (
                "cancelled" if exc.status == "cancelled" else "verification_unavailable" if stage == "verifying" else "failed")
            return terminal(status, error=str(exc), code=exc.status)
        finally:
            if completed_result is not None:
                await export_trace(self.config, run_id, graph_steps, completed_result)
