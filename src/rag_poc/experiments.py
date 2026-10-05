"""Predeclared verifier repeatability and real API/worker load measurements.

These tools never qualify a policy themselves. Fixtures exercise plumbing only;
live calls use the configured persistent budgets. Repeating paired replay timing
is deliberately not offered as an end-to-end latency measurement.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import math
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError, model_validator

from rag_poc.domain import CallContext, Contract, Draft, EvidencePack, ProviderError, stable_hash, strict_json
from rag_poc.evaluation import EvaluationDataset, implementation_fingerprint
from rag_poc.policy import evaluate_checks, semantic_policy_fingerprint


EXPERIMENT_VERSION = "experiments-v1"
SUCCESSFUL_LOAD = {"answered", "abstained", "shadow"}
TERMINAL_LOAD = SUCCESSFUL_LOAD | {"failed", "cancelled", "timed_out", "verification_unavailable"}
SAFE_CODES = {
    "invalid_response", "incomplete_coverage", "over_budget", "timeout", "provider_unavailable",
    "budget_exhausted", "cancelled", "storage_unavailable", "lease_lost", "space_changed",
}
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 4 * 1024 * 1024


class ExperimentError(ValueError):
    """Fixed, safe operator error; never include raw HTTP/config/source values."""


class FrozenInput(Contract):
    question_id: str
    question_hash: str
    answer_hash: str | None = None
    evidence_hash: str | None = None
    draft: Draft | None = None
    evidence: EvidencePack | None = None
    unavailable_reason: Literal["missing_initial_input"] | None = None

    @model_validator(mode="after")
    def exact_inputs(self):
        if self.unavailable_reason:
            if self.draft is not None or self.evidence is not None:
                raise ValueError("Unavailable inputs cannot also contain a draft or evidence")
        elif (self.draft is None or self.evidence is None
              or self.answer_hash != self.draft.content_hash or self.evidence_hash != self.evidence.content_hash):
            raise ValueError("Frozen input hashes must match the complete draft and evidence")
        return self


class ExperimentPlan(Contract):
    schema_version: Literal[1] = 1
    experiment_version: Literal["experiments-v1"] = EXPERIMENT_VERSION
    kind: Literal["repeatability", "live_load"]
    plan_id: str
    evaluation_id: str = Field(min_length=1)
    dataset_id: str
    dataset_hash: str
    config_hash: str
    semantic_fingerprint: str
    implementation_fingerprint: str
    profile: str
    model: str
    runtime_mode: Literal["mock", "live"]
    verification_mode: Literal["shadow", "evaluation", "gated"]
    fixture_only: bool
    predeclared_at: str
    question_ids: list[str] = Field(min_length=1)
    repetitions: int = 3
    concurrency: int = 1
    frozen_inputs: list[FrozenInput] = Field(default_factory=list)
    source_report_hash: str | None = None
    corpus_snapshots: dict[str, Any] = Field(default_factory=dict)
    cache_state: Literal["cold", "warm", "unspecified"] = "unspecified"
    cache_procedure: str = ""
    poll_interval_seconds: float = Field(default=0.05, gt=0, le=1)
    polling_grace_seconds: float = Field(default=2, ge=0, le=10)
    query_deadline_seconds: float = Field(gt=0)
    max_attempts_per_run: int = Field(ge=1)
    study_attempt_cap: int = Field(ge=1)

    @model_validator(mode="after")
    def protocol(self):
        if len(self.question_ids) != len(set(self.question_ids)):
            raise ValueError("Question IDs must be unique")
        _timestamp(self.predeclared_at)
        if self.kind == "repeatability":
            if self.repetitions != 3 or self.concurrency != 1:
                raise ValueError("Verifier repeatability uses three sequential repetitions")
            if not self.fixture_only and len(self.question_ids) != 20:
                raise ValueError("Live repeatability requires exactly twenty predeclared questions")
            if [row.question_id for row in self.frozen_inputs] != self.question_ids:
                raise ValueError("Every predeclared question requires one frozen input record")
        elif self.repetitions != 1 or self.concurrency != 4 or len(self.question_ids) < 4:
            raise ValueError("API load requires at least four questions and four concurrent journeys")
        if self.kind == "live_load" and len(self.question_ids) * self.max_attempts_per_run > self.study_attempt_cap:
            raise ValueError("The complete API load attempt ceiling must fit its declared study cap")
        if self.runtime_mode == "mock" and not self.fixture_only:
            raise ValueError("Mock runs cannot supply live qualification evidence")
        return self


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ExperimentError("Experiment timestamps must include a timezone.") from None


def _finite(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _signed(value: dict[str, Any], key: str) -> dict[str, Any]:
    payload = {k: v for k, v in value.items() if k != key}
    return {**payload, key: stable_hash(payload)}


def _decode_plan(value: dict[str, Any] | ExperimentPlan) -> ExperimentPlan:
    if isinstance(value, ExperimentPlan):
        return value
    try:
        payload = {k: v for k, v in value.items() if k != "plan_hash"}
        if value.get("plan_hash") != stable_hash(payload):
            raise ExperimentError("Experiment plan hash does not match its contents.")
        return ExperimentPlan.model_validate(payload)
    except ExperimentError:
        raise
    except (ValidationError, AttributeError, TypeError, ValueError):
        raise ExperimentError("Experiment plan violates its frozen protocol.") from None


def _selection(dataset: EvaluationDataset, question_ids: list[str], *, fixture_only: bool) -> list[Any]:
    if not isinstance(question_ids, list) or any(not isinstance(qid, str) for qid in question_ids):
        raise ExperimentError("Select explicit question IDs from the frozen dataset.")
    mapping = {case.id: case for case in dataset.questions}
    if len(question_ids) != len(set(question_ids)) or any(qid not in mapping for qid in question_ids):
        raise ExperimentError("Selected question IDs are missing or duplicated.")
    cases = [mapping[qid] for qid in question_ids]
    if not fixture_only and (dataset.purpose != "operator_evaluation" or any(case.split != "test" for case in cases)):
        raise ExperimentError("Live qualification experiments require the frozen held-out split.")
    return cases


def _plan_base(dataset, config, question_ids, evaluation_id, fixture_only):
    if config.runtime.mode == "mock" and not fixture_only:
        raise ExperimentError("Mock mode is fixture-only and cannot establish live qualification.")
    if dataset.purpose == "synthetic_fixture" and config.runtime.mode != "mock":
        raise ExperimentError("Synthetic fixture datasets may run only in mock mode.")
    _selection(dataset, question_ids, fixture_only=fixture_only)
    return {
        "plan_id": str(uuid.uuid4()), "evaluation_id": evaluation_id,
        "dataset_id": dataset.id, "dataset_hash": dataset.content_hash,
        "config_hash": config.fingerprint(), "semantic_fingerprint": semantic_policy_fingerprint(config),
        "implementation_fingerprint": implementation_fingerprint(),
        "profile": config.role_name("verifier"), "model": config.role_profile("verifier").model,
        "runtime_mode": config.runtime.mode, "verification_mode": config.verification.mode,
        "fixture_only": bool(fixture_only), "predeclared_at": _now(),
        "question_ids": list(question_ids),
        "query_deadline_seconds": float(config.runtime.query_deadline_seconds),
        "max_attempts_per_run": config.runtime.max_remote_attempts_per_query,
        "study_attempt_cap": config.evaluation.max_remote_attempts,
    }


def prepare_repeatability_plan(
    dataset: EvaluationDataset, config: Any, question_ids: list[str], *, evaluation_id: str,
    evaluation_report: dict[str, Any], fixture_only: bool = False,
) -> dict[str, Any]:
    """Freeze complete initial answers/evidence before any repeatability call.

    Use the same preselected IDs for each candidate profile. Missing original
    inputs remain planned failures; no replacement questions are sampled.
    """
    base = _plan_base(dataset, config, question_ids, evaluation_id, fixture_only)
    if evaluation_report.get("id") != evaluation_id or evaluation_report.get("dataset_hash") != dataset.content_hash:
        raise ExperimentError("The source evaluation report does not match the dataset and evaluation ID.")
    records = evaluation_report.get("questions")
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ExperimentError("The source evaluation report has invalid question records.")
    mapping = {row.get("id"): row for row in records}
    if len(mapping) != len(records):
        raise ExperimentError("The source evaluation report repeats question IDs.")
    frozen = []
    for case in _selection(dataset, question_ids, fixture_only=fixture_only):
        row = mapping.get(case.id, {})
        common = {"question_id": case.id, "question_hash": stable_hash(case.question)}
        if not row.get("initial_draft") or not row.get("evidence"):
            frozen.append(FrozenInput(**common, unavailable_reason="missing_initial_input"))
            continue
        try:
            draft = Draft.model_validate(row["initial_draft"])
            evidence = EvidencePack.model_validate(row["evidence"])
        except (ValidationError, TypeError, ValueError):
            raise ExperimentError("The source report contains malformed frozen inputs.") from None
        if (row.get("initial_draft_hash") != draft.content_hash or row.get("evidence_hash") != evidence.content_hash):
            raise ExperimentError("Source report hashes do not match its complete initial inputs.")
        frozen.append(FrozenInput(**common, draft=draft, evidence=evidence,
                                  answer_hash=draft.content_hash, evidence_hash=evidence.content_hash))
    try:
        plan = ExperimentPlan(**base, kind="repeatability", frozen_inputs=frozen,
                              source_report_hash=stable_hash(evaluation_report))
    except (ValidationError, ValueError):
        raise ExperimentError("Repeatability requires the declared twenty-question, three-repetition protocol.") from None
    return _signed(plan.model_dump(mode="json"), "plan_hash")


def _corpus_snapshots(store: Any, cases: list[Any]) -> dict[str, Any]:
    if store is None:
        raise ExperimentError("Live load planning requires access to the selected corpus snapshots.")
    try:
        result = {}
        for corpus_id in sorted({case.corpus_id for case in cases}):
            corpus = store.get_corpus(corpus_id)
            documents = store.list_documents(corpus_id)
            result[corpus_id] = {
                "space_id": corpus["space_id"], "revision": corpus["revision"],
                "active_versions": sorted(
                    [{"document_id": row.get("document_id") or row.get("id"), "version_id": row["active_version_id"]}
                     for row in documents if row.get("active_version_id")],
                    key=lambda row: row["document_id"],
                ),
            }
        return result
    except ExperimentError:
        raise
    except Exception:
        raise ExperimentError("Selected corpus snapshots could not be frozen safely.") from None


def prepare_load_plan(
    dataset: EvaluationDataset, config: Any, question_ids: list[str], *, evaluation_id: str,
    cache_state: Literal["cold", "warm", "unspecified"] = "unspecified", cache_procedure: str = "",
    fixture_only: bool = False, store: Any = None,
) -> dict[str, Any]:
    base = _plan_base(dataset, config, question_ids, evaluation_id, fixture_only)
    cases = _selection(dataset, question_ids, fixture_only=fixture_only)
    if not fixture_only and (cache_state == "unspecified" or not cache_procedure.strip()):
        raise ExperimentError("Declare cold or warm cache state and the actual preparation procedure before live load.")
    if any(len(case.question) > 16000 for case in cases):
        raise ExperimentError("A selected question exceeds the HTTP API input limit.")
    snapshots = _corpus_snapshots(store, cases) if store is not None or not fixture_only else {}
    try:
        plan = ExperimentPlan(**base, kind="live_load", repetitions=1, concurrency=4,
                              cache_state=cache_state, cache_procedure=cache_procedure,
                              corpus_snapshots=snapshots)
    except (ValidationError, ValueError):
        raise ExperimentError("API load requires at least four distinct predeclared question IDs.") from None
    return _signed(plan.model_dump(mode="json"), "plan_hash")


def _validate_bound_plan(value, dataset, config, kind) -> ExperimentPlan:
    plan = _decode_plan(value)
    if plan.kind != kind:
        raise ExperimentError("The selected plan is for a different experiment.")
    expected = (dataset.content_hash, config.fingerprint(), semantic_policy_fingerprint(config), implementation_fingerprint())
    actual = (plan.dataset_hash, plan.config_hash, plan.semantic_fingerprint, plan.implementation_fingerprint)
    if actual != expected or plan.runtime_mode != config.runtime.mode or plan.verification_mode != config.verification.mode:
        raise ExperimentError("Dataset, configuration, policy or implementation changed after predeclaration.")
    if _timestamp(plan.predeclared_at) > datetime.now(timezone.utc):
        raise ExperimentError("The predeclaration timestamp is later than execution time.")
    _selection(dataset, plan.question_ids, fixture_only=plan.fixture_only)
    if config.runtime.mode == "mock" and not plan.fixture_only:
        raise ExperimentError("Mock execution cannot be presented as live evidence.")
    return plan


def _live_admission(config, phase):
    if config.runtime.mode == "mock":
        return
    if config.budgets.total_max_estimated_cost_usd <= 0 or config.budgets.phase_max_estimated_cost_usd.get(phase, 0) <= 0:
        raise ExperimentError("Live experiments require positive total and matching phase budgets.")
    roles = ("verifier",) if phase == "evaluation" else ("embeddings", "generator", "verifier", "repair_generator")
    if any(config.role_profile(role).pricing is None for role in roles):
        raise ExperimentError("Live experiments require dated pricing for every profile they call.")


def _common_artifact(plan: ExperimentPlan, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "experiment_version": EXPERIMENT_VERSION,
        "kind": plan.kind, "artifact_id": str(uuid.uuid4()), "evaluation_id": plan.evaluation_id,
        "dataset_hash": plan.dataset_hash, "config_hash": plan.config_hash,
        "semantic_fingerprint": plan.semantic_fingerprint,
        "implementation_fingerprint": plan.implementation_fingerprint,
        "plan_hash": stable_hash(plan.model_dump(mode="json")), "plan": plan.model_dump(mode="json"),
        "profile": plan.profile, "model": plan.model, "runtime_mode": plan.runtime_mode,
        "verification_mode": plan.verification_mode, "fixture_only": plan.fixture_only,
        "predeclared_at": plan.predeclared_at, "started_at": started_at, "complete": False,
        "quality_qualified": False, "no_retuning": True,
    }


def _safe_call_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {"id", "call_id", "run_id", "phase", "profile", "protocol", "status", "usage",
               "estimated_cost", "charged_cost", "actual_cost", "estimated_cost_usd", "actual_cost_usd",
               "latency_seconds", "created_at", "finished_at", "cost_known"}
    return [{key: row[key] for key in allowed if key in row} for row in rows]


def _ledger(store: Any, prefix: str) -> list[dict[str, Any]]:
    if store is None:
        return []
    try:
        return store.get_calls(prefix=prefix)
    except TypeError:
        return [row for row in store.get_calls() if str(row.get("run_id", "")).startswith(prefix)]


def repeatability_summary(rows: list[dict[str, Any]], question_ids: list[str]) -> dict[str, Any]:
    questions = []
    for qid in question_ids:
        group = [row for row in rows if row.get("question_id") == qid]
        valid = [row for row in group if row.get("status") == "ok"]
        signatures = [row.get("check_signature") for row in valid]
        comparable = len(group) == 3 and len(valid) == 3 and {r.get("repetition") for r in group} == {1, 2, 3}
        questions.append({
            "question_id": qid, "planned_runs": 3, "completed_runs": len(valid),
            "failed_runs": 3 - len(valid), "comparable": comparable,
            "decision_disagreement": len({r["decision"] for r in valid}) > 1 if comparable else None,
            "per_check_disagreement": len(set(signatures)) > 1 if comparable else None,
            "input_hashes_unchanged": len({(r.get("answer_hash"), r.get("evidence_hash")) for r in group}) == 1,
        })
    comparable = [row for row in questions if row["comparable"]]
    return {
        "planned_run_denominator": len(question_ids) * 3, "recorded_runs": len(rows),
        "failed_runs": sum(row.get("status") != "ok" for row in rows),
        "question_denominator": len(question_ids), "comparable_questions": len(comparable),
        "decision_disagreement_questions": sum(row["decision_disagreement"] is True for row in comparable),
        "per_check_disagreement_questions": sum(row["per_check_disagreement"] is True for row in comparable),
        "questions": questions,
        "interpretation": "Exact fixed-input verifier disagreement; failures remain visible and are not evidence of agreement. No post-hoc disagreement threshold.",
    }


async def run_repeatability(plan, dataset, store, hub, config) -> dict[str, Any]:
    """Verify frozen answers three times; no generation, retrieval, or repair."""
    plan = _validate_bound_plan(plan, dataset, config, "repeatability")
    _live_admission(config, "evaluation")
    if config.runtime.mode == "live" and store is None:
        raise ExperimentError("Live repeatability requires the persistent call ledger.")
    if hasattr(hub, "config") and hub.config.fingerprint() != config.fingerprint():
        raise ExperimentError("The verifier hub does not match the frozen configuration.")
    prefix = f"repeat:{plan.plan_id}:"
    existing = _ledger(store, prefix)
    if existing:
        raise ExperimentError("This repeatability plan already has recorded attempts; it cannot be silently rerun.")
    cases = {case.id: case for case in dataset.questions}
    report = _common_artifact(plan, _now())
    report.update(measurement="verifier_repeatability", question_ids=plan.question_ids, repetitions=3,
                  repairs_disabled=True, fixed_inputs=True, runs=[], budget_phase="evaluation")
    consumed = 0
    stop_reason = None
    for repetition in (1, 2, 3):
        for frozen in plan.frozen_inputs:
            run_id = prefix + stable_hash(frozen.question_id)[:20] + f":{repetition}"
            row = {
                "question_id": frozen.question_id, "repetition": repetition, "run_id": run_id,
                "status": "not_attempted", "decision": "not_attempted", "answer_hash": frozen.answer_hash,
                "evidence_hash": frozen.evidence_hash, "attempts": 0, "attempted": False,
                "checks": [], "check_signature": None, "calls": [],
            }
            if stop_reason or consumed >= plan.study_attempt_cap:
                row.update(status=stop_reason or "budget_exhausted", decision="technical_failure")
                report["runs"].append(row)
                continue
            if frozen.unavailable_reason:
                row.update(status="missing_initial_input", decision="technical_failure")
                report["runs"].append(row)
                continue
            if stable_hash(cases[frozen.question_id].question) != frozen.question_hash:
                raise ExperimentError("A question changed after its input was frozen.")
            allowance = min(plan.max_attempts_per_run, plan.study_attempt_cap - consumed)
            ctx = CallContext.for_seconds(run_id, "evaluation", plan.query_deadline_seconds, allowance)
            started = time.monotonic()
            row["attempted"] = True
            try:
                async with asyncio.timeout(ctx.remaining()):
                    result = await hub.verify(cases[frozen.question_id].question, frozen.draft, frozen.evidence, ctx,
                                              round_id=f"repeatability.{repetition}")
                gate = evaluate_checks(frozen.draft, frozen.evidence, result, config.verification.score_threshold,
                                       expected_round_id=f"repeatability.{repetition}")
                if gate["technical_failures"]:
                    raise ProviderError("incomplete_coverage", "Repeated verifier response violated its frozen contract")
                checks = [{key: value[key] for key in ("id", "kind", "support_status", "check_status", "reason")}
                          for value in gate["checks"]]
                checks.sort(key=lambda value: value["id"])
                row.update(status="ok", decision="accepted" if gate["accepted"] else "rejected",
                           checks=checks, check_signature=stable_hash(checks))
            except asyncio.CancelledError:
                ctx.cancelled = True
                stop_reason = "cancelled"
                row.update(status="cancelled", decision="technical_failure")
            except (TimeoutError, httpx.TimeoutException):
                row.update(status="timeout", decision="technical_failure")
            except ProviderError as exc:
                code = exc.status if exc.status in SAFE_CODES else "provider_unavailable"
                row.update(status=code, decision="technical_failure")
                if code in {"budget_exhausted", "cancelled"}:
                    stop_reason = code
            except Exception:
                row.update(status="invalid_response", decision="technical_failure")
            finally:
                consumed += ctx.attempts_used
                row.update(attempts=ctx.attempts_used, elapsed_ms=(time.monotonic() - started) * 1000,
                           calls=_safe_call_rows(ctx.calls))
            report["runs"].append(row)
    report["summary"] = repeatability_summary(report["runs"], plan.question_ids)
    report["complete"] = len(report["runs"]) == len(plan.question_ids) * 3 and all(row["status"] == "ok" for row in report["runs"])
    report["remote_attempts"] = consumed
    report["ledger_calls"] = _safe_call_rows(_ledger(store, prefix))
    report["generated_at"] = _now()
    report["latency_note"] = "Verifier repeatability timings are not an end-to-end API/worker SLO measurement."
    return _signed(report, "content_hash")


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def _distribution(rows, field):
    values = [float(row[field]) for row in rows if _finite(row.get(field)) is not None]
    return {"planned_or_recorded_count": len(rows), "measured_count": len(values), "missing_count": len(rows) - len(values),
            "p50": _quantile(values, 0.5), "p95": _quantile(values, 0.95), "max": max(values) if values else None}


def load_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    strata = {}
    for cache in ("cold", "warm"):
        for repaired in (False, True):
            selected = [row for row in samples if row.get("cache_state") == cache and row.get("repair_attempted") is repaired]
            strata[f"{cache}:{'repair' if repaired else 'no_repair'}"] = {
                "count": len(selected), "failures": sum(row.get("status") not in SUCCESSFUL_LOAD for row in selected),
                "total_ms": _distribution(selected, "total_ms"), "queue_ms": _distribution(selected, "queue_ms"),
                "target_p95_ms": 40000 if repaired else 20000,
            }
    return {
        "sample_denominator": len(samples), "status_counts": dict(Counter(row.get("status", "unknown") for row in samples)),
        "technical_failures": sum(row.get("status") not in SUCCESSFUL_LOAD for row in samples),
        "completed_without_release": sum(row.get("status") == "shadow" for row in samples),
        "total_ms": _distribution(samples, "total_ms"), "queue_ms": _distribution(samples, "queue_ms"),
        "attempts_recorded": sum(row["attempts"] for row in samples if type(row.get("attempts")) is int),
        "unknown_attempt_rows": sum(type(row.get("attempts")) is not int for row in samples),
        "strata": strata,
        "interpretation": "All planned attempts stay in the sample, including failures. Empty or missing-timing strata establish no p95 target. Small strata provide no precision guarantee.",
    }


def _api_url(base_url: str) -> str:
    try:
        value = urlsplit(base_url)
        if (value.scheme not in {"http", "https"} or not value.hostname or value.username or value.password
                or value.query or value.fragment or any(ch.isspace() for ch in base_url)):
            raise ValueError
        _ = value.port
    except (TypeError, ValueError):
        raise ExperimentError("Use an explicit HTTP(S) API base URL without embedded credentials or query parameters.") from None
    return base_url.rstrip("/")


async def _api_json(client, method, url, *, headers, timeout, payload=None, params=None, expected=200):
    try:
        async with client.stream(method, url, headers=headers, json=payload, params=params, timeout=timeout, follow_redirects=False) as response:
            if response.status_code != expected:
                raise ExperimentError("The application API rejected an experiment request.")
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > MAX_API_RESPONSE_BYTES:
                    raise ExperimentError("Application API diagnostics exceeded their size limit.")
        value = strict_json(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ExperimentError("The application API returned an invalid object.")
        return value
    except (httpx.HTTPError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ExperimentError):
            raise
        raise ExperimentError("The application API could not complete a bounded request.") from None


async def _check_server(client, base_url, headers, plan):
    state = await _api_json(client, "GET", base_url + "/api/status", headers=headers, timeout=10)
    if state.get("mode") != plan.runtime_mode or state.get("config_fingerprint") != plan.config_hash:
        raise ExperimentError("The running API does not match the frozen runtime and configuration.")
    for corpus_id, snapshot in plan.corpus_snapshots.items():
        documents = await _api_json(client, "GET", base_url + "/api/documents", headers=headers, timeout=10,
                                    params={"corpus_id": corpus_id})
        rows = documents.get("documents")
        if not isinstance(rows, list):
            raise ExperimentError("The API returned invalid corpus diagnostics.")
        active = sorted(
            [{"document_id": row.get("document_id", row.get("id")), "version_id": row.get("active_version_id")}
             for row in rows if isinstance(row, dict) and row.get("active_version_id")],
            key=lambda row: row["document_id"] or "",
        )
        if active != snapshot["active_versions"]:
            raise ExperimentError("The active corpus changed after load predeclaration.")


def _repair_attempted(run: dict, config: Any) -> bool:
    timings = run.get("timings") or {}
    if "repair_generation_seconds" in timings or "verification_repair_seconds" in timings:
        return True
    for row in run.get("events", []):
        event = row.get("event", {}) if isinstance(row, dict) else {}
        if event.get("round_id") == "repair":
            return True
        gate = event.get("gate")
        if (event.get("type") == "verification" and event.get("round_id") == "initial"
                and isinstance(gate, dict) and not gate.get("accepted") and not gate.get("technical_failures")
                and config.verification.max_content_repairs > 0):
            return True
    return False


async def run_api_load(plan, dataset, config, base_url: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Run four concurrent actual HTTP query journeys, including queue time.

    A prequalification shadow response completes the pipeline without releasing
    an answer. Operator-declared cold/warm cache preparation is recorded, never
    inferred or manufactured by this client. No failed POST is retried.
    """
    plan = _validate_bound_plan(plan, dataset, config, "live_load")
    _live_admission(config, "queries")
    base_url = _api_url(base_url)
    headers = {}
    if config.runtime.operator_token is not None:
        headers["Authorization"] = "Bearer " + config.runtime.operator_token.get_secret_value()
    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=8))
    report = _common_artifact(plan, _now())
    report.update(measurement="api_worker_end_to_end", concurrency=4, samples=[], budget_phase="queries",
                  cache_preparation={"state": plan.cache_state, "operator_procedure": plan.cache_procedure, "independently_verified": False})
    cases = _selection(dataset, plan.question_ids, fixture_only=plan.fixture_only)
    semaphore = asyncio.Semaphore(4)
    first_wave = asyncio.Event()
    active = peak = 0
    slots: list[dict[str, Any] | None] = [None] * len(cases)

    async def one(index, case):
        nonlocal active, peak
        async with semaphore:
            active += 1
            peak = max(peak, active)
            if active == 4:
                first_wave.set()
            await first_wave.wait()
            started = time.monotonic()
            deadline = started + plan.query_deadline_seconds + plan.polling_grace_seconds
            row = {"question_id": case.id, "run_id": None, "job_id": None, "cache_state": plan.cache_state,
                   "repair_attempted": None, "status": "not_attempted", "queue_ms": None, "total_ms": None,
                   "client_observed_total_ms": None, "attempts": None, "calls": [], "timings_ms": {}, "error_code": None,
                   "submission_outcome": "unknown", "submission_retried": False}
            terminal_seen = False
            try:
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    run = await _api_json(client, "POST", base_url + "/api/queries", headers=headers,
                                          timeout=max(0.001, deadline - time.monotonic()),
                                          payload={"question": case.question, "corpus_id": case.corpus_id}, expected=202)
                    if not isinstance(run.get("id"), str) or not run["id"] or not isinstance(run.get("job_id"), str):
                        raise ExperimentError("The application did not return durable run and job IDs.")
                    row.update(run_id=run["id"], job_id=run["job_id"], submission_outcome="acknowledged")
                    while run.get("status") not in TERMINAL_LOAD:
                        await asyncio.sleep(min(plan.poll_interval_seconds, max(0, deadline - time.monotonic())))
                        run = await _api_json(client, "GET", base_url + "/api/runs/" + row["run_id"],
                                             headers=headers, timeout=max(0.001, deadline - time.monotonic()))
                        if run.get("id") != row["run_id"]:
                            raise ExperimentError("The API returned a different run during polling.")
                    terminal_seen = True
                    row["total_ms"] = (time.monotonic() - started) * 1000
                    row["client_observed_total_ms"] = row["total_ms"]
                    trace = await _api_json(client, "GET", base_url + "/api/runs/" + row["run_id"] + "/trace",
                                           headers=headers, timeout=max(0.001, deadline - time.monotonic()))
                    raw = trace.get("run")
                    calls = trace.get("calls")
                    if (not isinstance(raw, dict) or raw.get("id") != row["run_id"] or not isinstance(calls, list)
                            or any(not isinstance(call, dict) or call.get("run_id") != row["run_id"] for call in calls)):
                        raise ExperimentError("Run diagnostics do not match the durable query ID.")
                    if raw.get("config_fingerprint") != plan.config_hash or raw.get("mode") != plan.runtime_mode:
                        raise ExperimentError("A measured run used a different configuration or runtime.")
                    timings = raw.get("timings", {})
                    if not isinstance(timings, dict):
                        raise ExperimentError("The completed run has invalid timing diagnostics.")
                    queue = _finite(timings.get("queue_seconds"))
                    total = _finite(timings.get("total_seconds"))
                    if queue is None or total is None or queue > total:
                        raise ExperimentError("The completed run lacks valid queue-inclusive timing.")
                    snapshot = plan.corpus_snapshots.get(case.corpus_id)
                    evidence = raw.get("evidence")
                    if snapshot and raw["status"] in SUCCESSFUL_LOAD and (
                        not isinstance(evidence, dict) or evidence.get("space_id") != snapshot["space_id"]
                        or evidence.get("corpus_revision") != snapshot["revision"]
                    ):
                        raise ExperimentError("A measured run used a changed corpus snapshot.")
                    row.update(status=raw["status"], queue_ms=queue * 1000,
                               total_ms=max(row["total_ms"], total * 1000), worker_reported_total_ms=total * 1000,
                               repair_attempted=_repair_attempted(raw, config), attempts=len(calls),
                               calls=_safe_call_rows(calls), timings_ms={key: val * 1000 for key, val in timings.items() if _finite(val) is not None},
                               completion_without_release=raw["status"] == "shadow")
            except asyncio.CancelledError:
                row.update(status="cancelled", error_code="cancelled")
            except (TimeoutError, httpx.TimeoutException):
                row.update(status="timed_out", error_code="client_deadline")
            except ExperimentError:
                row.update(status="failed", error_code="api_contract_or_transport")
            except Exception:
                row.update(status="failed", error_code="experiment_failure")
            finally:
                if row["total_ms"] is None:
                    row["total_ms"] = (time.monotonic() - started) * 1000
                    row["client_observed_total_ms"] = row["total_ms"]
                if not terminal_seen and row["job_id"] is not None:
                    try:
                        await _api_json(client, "POST", base_url + "/api/jobs/" + row["job_id"] + "/cancel",
                                        headers=headers, timeout=2)
                        row["cancellation_requested"] = True
                    except Exception:
                        row["cancellation_requested"] = False
                slots[index] = row
                active -= 1

    try:
        await _check_server(client, base_url, headers, plan)
        tasks = [asyncio.create_task(one(index, case)) for index, case in enumerate(cases)]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await _check_server(client, base_url, headers, plan)
            unchanged = True
        except ExperimentError:
            unchanged = False
        for index, row in enumerate(slots):
            report["samples"].append(row or {
                "question_id": cases[index].id, "run_id": None, "cache_state": plan.cache_state,
                "repair_attempted": None, "status": "cancelled", "queue_ms": None, "total_ms": None,
                "attempts": None, "error_code": "cancelled_before_submission",
            })
        report["server_configuration_unchanged"] = unchanged
        report["max_inflight_runs"] = peak
        report["complete"] = unchanged and peak == 4 and all(
            row["status"] in SUCCESSFUL_LOAD and row["run_id"] and type(row["attempts"]) is int
            and row["queue_ms"] is not None for row in report["samples"]
        )
        report["summary"] = load_summary(report["samples"])
        report["generated_at"] = _now()
        report["latency_note"] = "Client-monotonic HTTP submission to observed terminal response includes queue and polling; diagnostic trace fetch is excluded. total_ms is the conservative maximum of that observed duration and server queue+execution time; both measurements are retained. Shadow completion measures pipeline latency without releasing an answer."
        return _signed(report, "content_hash")
    finally:
        if owns_client:
            await client.aclose()


def merge_load_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine declared cold/warm campaigns; never invent absent repair strata."""
    if not reports:
        raise ExperimentError("Supply at least one completed load campaign artifact.")
    identity = ("evaluation_id", "dataset_hash", "config_hash", "semantic_fingerprint", "implementation_fingerprint",
                "profile", "model", "runtime_mode", "verification_mode", "fixture_only", "concurrency")
    first = reports[0]
    samples = []
    seen = set()
    for report in reports:
        payload = {key: value for key, value in report.items() if key != "content_hash"}
        if report.get("content_hash") != stable_hash(payload) or report.get("kind") != "live_load":
            raise ExperimentError("A load campaign has an invalid content hash or kind.")
        if any(report.get(key) != first.get(key) for key in identity):
            raise ExperimentError("Load campaigns do not share the same frozen identity.")
        for row in report.get("samples", []):
            run_id = row.get("run_id")
            if run_id is not None and run_id in seen:
                raise ExperimentError("The same durable run cannot count twice in a load study.")
            if run_id is not None:
                seen.add(run_id)
            samples.append(row)
    result = {
        "schema_version": 1, "experiment_version": EXPERIMENT_VERSION, "kind": "live_load",
        "artifact_id": str(uuid.uuid4()), **{key: first.get(key) for key in identity},
        "measurement": "api_worker_end_to_end", "complete": all(report.get("complete") is True for report in reports),
        "quality_qualified": False, "samples": samples, "summary": load_summary(samples),
        "started_at": min(report["started_at"] for report in reports), "generated_at": _now(),
        "source_artifacts": [{"artifact_id": report["artifact_id"], "content_hash": report["content_hash"],
                              "plan_hash": report["plan_hash"], "cache_preparation": report.get("cache_preparation")}
                             for report in reports],
        "latency_note": first.get("latency_note"),
    }
    return _signed(result, "content_hash")


def load_experiment_file(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ExperimentError("Experiment file exceeds the bounded artifact size.")
        result = strict_json(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ExperimentError("Experiment file must contain one JSON object.")
        return result
    except ExperimentError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ExperimentError("Experiment file could not be read safely.") from None


def save_experiment_file(value: dict[str, Any], path: str | Path) -> str:
    """Save an explicit operator artifact without overwriting an earlier run."""
    try:
        raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ExperimentError("Experiment artifact exceeds the bounded output size.")
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(raw)
        return str(destination)
    except ExperimentError:
        raise
    except (OSError, TypeError, ValueError):
        raise ExperimentError("Experiment artifact could not be saved; choose a new writable output path.") from None


def estimate_experiment(plan, dataset, config) -> dict[str, Any]:
    """Validate a plan and compute conservative ceilings without model/API work."""
    decoded = _decode_plan(plan)
    decoded = _validate_bound_plan(plan, dataset, config, decoded.kind)
    from rag_poc.providers import ProviderHub
    from rag_poc.providers.transport import check_payload, output_reservation, payload_limit
    known = True
    estimated = 0.0
    preflight_failures = 0
    if decoded.kind == "repeatability":
        profile = config.role_profile("verifier")
        hub = ProviderHub(config)
        questions = {case.id: case.question for case in dataset.questions}
        attempted_ceiling = 0
        for frozen in decoded.frozen_inputs:
            if frozen.unavailable_reason:
                preflight_failures += 3
                continue
            try:
                hub._validate_draft_size(frozen.draft)
                payload = hub._verification_payload(profile, questions[frozen.question_id], frozen.draft, frozen.evidence)
                tokens = check_payload(profile, payload)
            except ProviderError:
                preflight_failures += 3
                continue
            attempts = 3 * min(profile.max_attempts, decoded.max_attempts_per_run)
            attempted_ceiling += attempts
            if profile.pricing is None:
                known = False
            else:
                estimated += attempts * (tokens * profile.pricing.input_usd_per_million
                                         + output_reservation(profile) * profile.pricing.output_usd_per_million) / 1_000_000
        phase = "evaluation"
        logical_runs = len(decoded.question_ids) * 3
    else:
        values = []
        for role in ("embeddings", "generator", "verifier", "repair_generator"):
            profile = config.role_profile(role)
            if profile.pricing is None:
                known = False
            else:
                values.append((max(0, payload_limit(profile)) * profile.pricing.input_usd_per_million
                               + output_reservation(profile) * profile.pricing.output_usd_per_million) / 1_000_000)
        attempted_ceiling = len(decoded.question_ids) * decoded.max_attempts_per_run
        estimated = attempted_ceiling * max(values, default=0)
        phase = "queries"
        logical_runs = len(decoded.question_ids)
    if config.runtime.mode == "mock":
        estimated, known = 0.0, True
    return {
        "kind": decoded.kind, "plan_hash": stable_hash(decoded.model_dump(mode="json")),
        "dry_run": True, "runtime_mode": decoded.runtime_mode, "fixture_only": decoded.fixture_only,
        "logical_runs": logical_runs, "remote_attempt_ceiling": attempted_ceiling,
        "study_attempt_cap": decoded.study_attempt_cap, "preflight_failure_runs": preflight_failures,
        "pricing_known": known, "estimated_upper_bound_usd": estimated if known else None,
        "budget_phase": phase, "total_cap_usd": config.budgets.total_max_estimated_cost_usd,
        "phase_cap_usd": config.budgets.phase_max_estimated_cost_usd.get(phase, 0),
        "note": "A conservative ceiling, not a bill or budget reservation. Each live request reserves cost in the durable ledger; unknown usage remains charged conservatively. No network/model calls were made by this estimate.",
    }


def main(argv: list[str] | None = None) -> int:
    """CLI bridge used by ``rag-poc experiments``; paid execution is explicit."""
    from rag_poc.config import ConfigError, load_config
    from rag_poc.evaluation import DatasetError, load_dataset
    parser = argparse.ArgumentParser(description="Predeclared experiments; run commands default to dry-run.")
    parser.add_argument("--config", default="configs/mock.yaml")
    parser.add_argument("--verifier-profile", help="Explicitly activate this verifier; preserves the native-protocol opt-in flag")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_repeat = commands.add_parser("prepare-repeatability")
    prepare_repeat.add_argument("dataset", type=Path)
    prepare_repeat.add_argument("evaluation_report", type=Path)
    prepare_repeat.add_argument("--question-id", action="append", required=True)
    prepare_repeat.add_argument("--fixture-only", action="store_true")
    prepare_repeat.add_argument("--output", type=Path, required=True)
    prepare_load = commands.add_parser("prepare-load")
    prepare_load.add_argument("dataset", type=Path)
    prepare_load.add_argument("--evaluation-id", required=True)
    prepare_load.add_argument("--question-id", action="append", required=True)
    prepare_load.add_argument("--cache-state", choices=["cold", "warm", "unspecified"], default="unspecified")
    prepare_load.add_argument("--cache-procedure", default="")
    prepare_load.add_argument("--fixture-only", action="store_true")
    prepare_load.add_argument("--output", type=Path, required=True)
    for command in ("run-repeatability", "run-load"):
        child = commands.add_parser(command)
        child.add_argument("dataset", type=Path)
        child.add_argument("plan", type=Path)
        child.add_argument("--execute", action="store_true", help="Execute model/API query calls under configured budgets")
        child.add_argument("--output", type=Path)
        if command == "run-load":
            child.add_argument("--base-url", default="http://127.0.0.1:8000")
    merge = commands.add_parser("merge-load")
    merge.add_argument("reports", type=Path, nargs="+")
    merge.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "merge-load":
            result = merge_load_reports([load_experiment_file(path) for path in args.reports])
            saved = save_experiment_file(result, args.output)
            print(json.dumps({"output": saved, "complete": result["complete"], "summary": result["summary"]}, indent=2))
            return 0
        config = load_config(args.config)
        if args.verifier_profile:
            config = config.with_roles(verifier=args.verifier_profile)
        dataset = load_dataset(args.dataset)
        if args.command == "prepare-repeatability":
            source = load_experiment_file(args.evaluation_report)
            result = prepare_repeatability_plan(dataset, config, args.question_id,
                                                evaluation_id=source.get("id", ""), evaluation_report=source,
                                                fixture_only=args.fixture_only)
            saved = save_experiment_file(result, args.output)
            print(json.dumps({"output": saved, **estimate_experiment(result, dataset, config)}, indent=2))
            return 0
        if args.command == "prepare-load":
            from rag_poc.storage import Store
            store = Store(config.database.dsn, limits=config.ingestion.model_dump(mode="json"))
            result = prepare_load_plan(dataset, config, args.question_id, evaluation_id=args.evaluation_id,
                                       cache_state=args.cache_state, cache_procedure=args.cache_procedure,
                                       fixture_only=args.fixture_only, store=store)
            saved = save_experiment_file(result, args.output)
            print(json.dumps({"output": saved, **estimate_experiment(result, dataset, config)}, indent=2))
            return 0
        plan = load_experiment_file(args.plan)
        expected_kind = "repeatability" if args.command == "run-repeatability" else "live_load"
        _validate_bound_plan(plan, dataset, config, expected_kind)
        estimate = estimate_experiment(plan, dataset, config)
        print(json.dumps(estimate, indent=2))
        if not args.execute:
            return 0
        if args.output is None or args.output.exists():
            raise ExperimentError("Execution requires a new, explicit output path to preserve the resulting artifact.")
        if args.command == "run-repeatability":
            from rag_poc.providers import ProviderHub
            from rag_poc.storage import Store
            store = Store(config.database.dsn, limits=config.ingestion.model_dump(mode="json"))
            async def execute_repeat():
                hub = ProviderHub(config, store=store)
                try:
                    return await run_repeatability(plan, dataset, store, hub, config)
                finally:
                    await hub.aclose()
            result = asyncio.run(execute_repeat())
        else:
            result = asyncio.run(run_api_load(plan, dataset, config, args.base_url))
        saved = save_experiment_file(result, args.output)
        print(json.dumps({"output": saved, "complete": result["complete"], "summary": result["summary"]}, indent=2))
        return 0 if result["complete"] else 1
    except (ExperimentError, ConfigError, DatasetError, ProviderError, OSError, ValueError):
        print(json.dumps({"error": "Experiment configuration, frozen inputs, API or output contract failed. No policy was qualified."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
