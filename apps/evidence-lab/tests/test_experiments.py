"""Offline experiment protocol tests, never live latency or model-quality evidence."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Literal

import httpx
import pytest

from evidence_lab.config import AppConfig, load_config
from evidence_lab.domain import AnswerBlock, CheckResult, Draft, EvidenceItem, EvidencePack, ProviderError, stable_hash
from evidence_lab.evaluation import (
    EvaluationDataset, _load_artifact_checks, _repeatability_artifact_checks, implementation_fingerprint,
)
from evidence_lab.experiments import (
    ExperimentError, estimate_experiment, load_experiment_file, main, merge_load_reports,
    prepare_load_plan, prepare_repeatability_plan, run_api_load, run_repeatability, save_experiment_file,
)
from evidence_lab.policy import semantic_policy_fingerprint
from evidence_lab.providers import ProviderHub
from evidence_lab.providers.mock import verify as fixture_verify


ROOT = Path(__file__).resolve().parents[3]
SECRET = "experiment-test-secret-never-disclose"


def configuration(*, live=False, mode="shadow", attempt_cap=3000):
    raw = load_config(ROOT / "configs/mock.yaml").model_dump(mode="python")
    raw["verification"].update(mode=mode, policy_id=None)
    raw["runtime"]["operator_token"] = SECRET
    raw["evaluation"]["max_remote_attempts"] = attempt_cap
    if live:
        raw["runtime"]["mode"] = "live"
        raw["budgets"] = {
            "total_max_estimated_cost_usd": 10.0,
            "phase_max_estimated_cost_usd": {"ingestion": 5.0, "queries": 5.0, "smoke": 5.0, "evaluation": 5.0},
        }
        for index, profile in enumerate(raw["profiles"].values()):
            profile.update(endpoint=f"https://unit.test/operation/{index}", model=f"configured-model-{index}",
                           api_key=SECRET, pricing={"input_usd_per_million": 1.0, "output_usd_per_million": 2.0,
                                                  "checked_on": "2026-10-05"})
    return AppConfig.model_validate(raw)


def dataset(*, count=20, live=False, split="test"):
    return EvaluationDataset.model_validate({
        "id": "experiment-fixture", "version": "1", "title": "Offline experiment contract fixtures",
        "purpose": "operator_evaluation" if live else "synthetic_fixture",
        "families": [{"id": "family-a", "split": split}],
        "questions": [{"id": f"q{index:02}", "family_id": "family-a", "split": split,
                       "question": f"What is the measured value for sample {index}?",
                       "expected_response": "answerable", "corpus_id": "corpus-a"}
                      for index in range(1, count + 1)],
    })


def source_report(data, config):
    rows = []
    for index, case in enumerate(data.questions, 1):
        text = f"The measured value for sample {index} is {index * 7}."
        evidence = EvidencePack(corpus_id=case.corpus_id, space_id="fixture-space", corpus_revision=3,
                                items=[EvidenceItem(id=f"e{index}", document_id="document-a", version_id="version-a",
                                                    title="Fixture table", text=text, end=len(text),
                                                    text_hash=hashlib.sha256(text.encode()).hexdigest())])
        draft = Draft(blocks=[AnswerBlock(block_id="b1", text=text, citation_ids=[f"e{index}"])])
        rows.append({"id": case.id, "initial_draft": draft.model_dump(mode="json"), "initial_draft_hash": draft.content_hash,
                     "evidence": evidence.model_dump(mode="json"), "evidence_hash": evidence.content_hash})
    return {"id": "evaluation-original", "dataset_hash": data.content_hash, "questions": rows,
            "identity": {"policy_hash": semantic_policy_fingerprint(config), "implementation_fingerprint": implementation_fingerprint()}}


def repeat_plan(data, config, *, source=None):
    return prepare_repeatability_plan(data, config, [case.id for case in data.questions],
                                      evaluation_id="evaluation-original", evaluation_report=source or source_report(data, config),
                                      fixture_only=config.runtime.mode == "mock")


def load_plan(data, config, *, cache: Literal["cold", "warm", "unspecified"] = "cold", store=None):
    return prepare_load_plan(data, config, [case.id for case in data.questions], evaluation_id="evaluation-original",
                             cache_state=cache, cache_procedure=f"Offline HTTP fixture labelled {cache}; no real cache measurement.",
                             fixture_only=config.runtime.mode == "mock", store=store)


def reseal(value, key="plan_hash"):
    payload = {name: item for name, item in value.items() if name != key}
    return {**payload, key: stable_hash(payload)}


class Ledger:
    """Only a test double; production experiments require Store's durable ledger."""

    def __init__(self):
        self.rows = []

    def get_calls(self, run_id=None, *, prefix=None):
        return [row for row in self.rows if (run_id is None or row["run_id"] == run_id)
                and (prefix is None or row["run_id"].startswith(prefix))]

    def reserve_call(self, run_id, phase, profile, estimated_cost, limits):
        call_id = str(len(self.rows))
        self.rows.append({"id": call_id, "run_id": run_id, "phase": phase, "profile": profile,
                          "estimated_cost": estimated_cost, "status": "reserved", "limits": limits})
        return call_id

    def finish_call(self, call_id, status, usage=None, actual_cost=None, detail=None):
        self.rows[int(call_id)].update(status=status, usage=usage, actual_cost=actual_cost, detail=detail)


class FixedInputVerifier:
    def __init__(self, config, *, disagree=False, failure_at=None, failure="budget_exhausted"):
        self.config = config
        self.disagree = disagree
        self.failure_at = failure_at
        self.failure = failure
        self.calls = []

    async def verify(self, question, draft, evidence, ctx, round_id="initial"):
        ctx.consume_attempt()
        self.calls.append((question, draft.content_hash, evidence.content_hash, ctx.phase, round_id, ctx.max_attempts))
        ctx.calls.append({"run_id": ctx.run_id, "phase": ctx.phase, "status": "ok", "api_key": SECRET,
                          "authorization": SECRET, "raw_response": SECRET})
        if self.failure_at == len(self.calls):
            raise ProviderError(self.failure, SECRET)
        result = fixture_verify(question, draft, evidence, round_id)
        if self.disagree and question.endswith("sample 1?") and round_id == "repeatability.2":
            result.checks[0] = CheckResult(id="b1", kind="block_support", support_status="not_supported", reason="insufficient_evidence")
        return result


class SnapshotStore:
    def get_corpus(self, corpus_id):
        return {"id": corpus_id, "space_id": "fixture-space", "revision": 3}

    def list_documents(self, corpus_id):
        # Real contract producers may return document_id without an additional id.
        return [{"document_id": "document-a", "active_version_id": "version-a"}]


def test_live_predeclaration_freezes_exact_twenty_heldout_inputs_without_calls():
    config, data = configuration(live=True), dataset(live=True)
    source = source_report(data, config)
    plan = repeat_plan(data, config, source=source)
    assert len(plan["question_ids"]) == 20 and plan["repetitions"] == 3
    assert plan["source_report_hash"] == stable_hash(source)
    assert plan["semantic_fingerprint"] == source["identity"]["policy_hash"]
    assert all(row["answer_hash"] == stable_hash(row["draft"]) and row["evidence_hash"] == stable_hash(row["evidence"])
               for row in plan["frozen_inputs"])
    assert SECRET not in json.dumps(plan)
    estimate = estimate_experiment(plan, data, config)
    assert estimate["dry_run"] is True and estimate["logical_runs"] == 60
    assert estimate["budget_phase"] == "evaluation" and estimate["estimated_upper_bound_usd"] > 0
    assert estimate["preflight_failure_runs"] == 0


@pytest.mark.parametrize("change", ["nineteen", "duplicate", "development", "changed_source_hash"])
def test_live_predeclaration_rejects_changed_sample_or_inputs(change):
    config = configuration(live=True)
    data = dataset(live=True, count=19 if change == "nineteen" else 20, split="development" if change == "development" else "test")
    source = source_report(data, config)
    ids = [case.id for case in data.questions]
    if change == "duplicate":
        ids[-1] = ids[0]
    if change == "changed_source_hash":
        source["questions"][0]["initial_draft"]["blocks"][0]["text"] = "Changed after recording."
    with pytest.raises(ExperimentError):
        prepare_repeatability_plan(data, config, ids, evaluation_id=source["id"], evaluation_report=source)


def test_mock_predeclaration_requires_explicit_fixture_marker():
    config, data = configuration(), dataset()
    with pytest.raises(ExperimentError, match="fixture-only"):
        prepare_repeatability_plan(data, config, [case.id for case in data.questions],
                                   evaluation_id="evaluation-original", evaluation_report=source_report(data, config))


@pytest.mark.asyncio
async def test_twenty_by_three_real_mock_hub_calls_use_evaluation_ledger_and_cannot_qualify():
    config, data, ledger = configuration(), dataset(), Ledger()
    source = source_report(data, config)
    plan = repeat_plan(data, config, source=source)

    def forbidden(_request):
        raise AssertionError("Experiment fixtures must never make remote requests")

    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(forbidden))
    try:
        report = await run_repeatability(plan, data, ledger, hub, config)
    finally:
        await hub.aclose()
    assert report["complete"] is True and len(report["runs"]) == 60
    assert report["remote_attempts"] == 60 == len(ledger.rows)
    assert {row["phase"] for row in ledger.rows} == {"evaluation"}
    assert {row["profile"] for row in ledger.rows} == {config.roles.verifier}
    assert len({row["run_id"] for row in report["runs"]}) == 60
    assert {(row["question_id"], row["repetition"]) for row in report["runs"]} == {
        (case.id, repetition) for case in data.questions for repetition in (1, 2, 3)}
    assert report["summary"]["decision_disagreement_questions"] == 0
    assert report["summary"]["comparable_questions"] == 20
    assert report["fixture_only"] is True and report["quality_qualified"] is False
    check = _repeatability_artifact_checks(report, source)
    assert check["passed"] is False and any("live endpoints" in error for error in check["errors"])
    with pytest.raises(ExperimentError, match="already has recorded attempts"):
        await run_repeatability(plan, data, ledger, hub, config)


@pytest.mark.asyncio
async def test_verifier_disagreement_keeps_the_same_answer_evidence_and_threshold():
    config, data = configuration(), dataset()
    hub = FixedInputVerifier(config, disagree=True)
    report = await run_repeatability(repeat_plan(data, config), data, None, hub, config)
    assert report["complete"] is True
    assert len(hub.calls) == 60 and {call[3] for call in hub.calls} == {"evaluation"}
    assert len({call[1:3] for call in hub.calls}) == 20
    assert report["summary"]["decision_disagreement_questions"] == 1
    assert report["summary"]["per_check_disagreement_questions"] == 1
    assert all(row["input_hashes_unchanged"] for row in report["summary"]["questions"])
    assert {row["answer_hash"] for row in report["runs"] if row["question_id"] == "q01"} == {hub.calls[0][1]}
    assert SECRET not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["budget_exhausted", "timeout"])
async def test_remote_failures_remain_in_full_predeclared_denominator(reason):
    config, data = configuration(), dataset()
    hub = FixedInputVerifier(config, failure_at=3, failure=reason)
    report = await run_repeatability(repeat_plan(data, config), data, None, hub, config)
    assert report["complete"] is False and len(report["runs"]) == 60
    assert report["summary"]["planned_run_denominator"] == 60
    expected_calls = 3 if reason == "budget_exhausted" else 60
    assert len(hub.calls) == report["remote_attempts"] == expected_calls
    assert all(row["answer_hash"] and row["evidence_hash"] for row in report["runs"])
    if reason == "budget_exhausted":
        assert report["summary"]["failed_runs"] == 58
        assert report["summary"]["comparable_questions"] == 0
    assert SECRET not in json.dumps(report)


@pytest.mark.asyncio
async def test_shared_repeatability_attempt_cap_prevents_new_remote_calls():
    config, data = configuration(attempt_cap=2), dataset()
    hub = FixedInputVerifier(config)
    report = await run_repeatability(repeat_plan(data, config), data, None, hub, config)
    assert len(hub.calls) == report["remote_attempts"] == 2
    assert [call[-1] for call in hub.calls] == [2, 1]
    assert len(report["runs"]) == 60 and report["complete"] is False
    assert sum(row["attempted"] for row in report["runs"]) == 2


@pytest.mark.asyncio
async def test_missing_initial_input_keeps_three_failed_slots_without_replacement():
    config, data = configuration(), dataset()
    source = source_report(data, config)
    source["questions"][0]["initial_draft"] = None
    hub = FixedInputVerifier(config)
    plan = repeat_plan(data, config, source=source)
    assert estimate_experiment(plan, data, config)["preflight_failure_runs"] == 3
    report = await run_repeatability(plan, data, None, hub, config)
    assert len(hub.calls) == 57 and report["complete"] is False
    missing = [row for row in report["runs"] if row["question_id"] == "q01"]
    assert len(missing) == 3 and {row["status"] for row in missing} == {"missing_initial_input"}
    assert report["summary"]["comparable_questions"] == 19


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["plan_hash", "dataset", "config", "implementation"])
async def test_execution_rejects_identity_drift_before_calling_verifier(change, monkeypatch):
    config, data = configuration(), dataset()
    plan = repeat_plan(data, config)
    if change == "plan_hash":
        plan["question_ids"].reverse()
    elif change == "dataset":
        data.questions[0].question = "Changed after predeclaration?"
    elif change == "config":
        config.verification.max_content_repairs = 0
    else:
        monkeypatch.setattr("evidence_lab.experiments.implementation_fingerprint", lambda: "changed-code")
    hub = FixedInputVerifier(config)
    with pytest.raises(ExperimentError):
        await run_repeatability(plan, data, None, hub, config)
    assert hub.calls == []


def test_live_load_requires_declared_cache_snapshot_and_conservative_total_attempt_cap():
    config, data = configuration(live=True), dataset(count=4, live=True)
    ids = [case.id for case in data.questions]
    with pytest.raises(ExperimentError, match="cache state"):
        prepare_load_plan(data, config, ids, evaluation_id="evaluation-original", store=SnapshotStore())
    with pytest.raises(ExperimentError, match="snapshot"):
        prepare_load_plan(data, config, ids, evaluation_id="evaluation-original", cache_state="cold", cache_procedure="Operator restart.")
    plan = load_plan(data, config, store=SnapshotStore())
    assert plan["corpus_snapshots"]["corpus-a"]["active_versions"] == [{"document_id": "document-a", "version_id": "version-a"}]
    config.evaluation.max_remote_attempts = 39
    with pytest.raises(ExperimentError):
        load_plan(data, config, store=SnapshotStore())


class FixtureAPI:
    """In-process HTTP protocol fixture. Its timings are deliberately not SLO evidence."""

    def __init__(self, config, *, namespace="cold", ambiguous=None, failure=None, hanging=None, drift=False, missing_queue=False):
        self.config = config
        self.namespace = namespace
        self.ambiguous = ambiguous
        self.failure = failure
        self.hanging = hanging
        self.drift = drift
        self.missing_queue = missing_queue
        self.submissions = []
        self.runs = {}
        self.active = self.peak = self.status_reads = 0
        self.cancellations = []

    async def __call__(self, request):
        assert request.headers["Authorization"] == "Bearer " + SECRET
        path = request.url.path
        if path == "/api/status":
            self.status_reads += 1
            fingerprint = "changed" if self.drift and self.status_reads > 1 else self.config.fingerprint()
            return httpx.Response(200, json={"mode": self.config.runtime.mode, "config_fingerprint": fingerprint})
        if path == "/api/documents":
            return httpx.Response(200, json={"documents": SnapshotStore().list_documents("corpus-a")})
        if path == "/api/queries":
            payload = json.loads(request.content)
            self.submissions.append(payload)
            number = len(self.submissions)
            run_id = f"{self.namespace}-run-{number}"
            job_id = f"{self.namespace}-job-{number}"
            is_failed = number == self.failure
            is_repair = number % 2 == 0
            raw = {"id": run_id, "job_id": job_id, "status": "failed" if is_failed else "shadow",
                   "mode": self.config.runtime.mode, "config_fingerprint": self.config.fingerprint(),
                   "timings": {"queue_seconds": 0.001, "total_seconds": 0.010 + number / 1000},
                   "events": [], "evidence": {"space_id": "fixture-space", "corpus_revision": 3}}
            if self.missing_queue and number == 1:
                raw["timings"].pop("queue_seconds")
            if is_repair:
                # Include a repair that was attempted even when repair generation itself fails.
                raw["events"] = [{"event": {"type": "verification", "round_id": "initial",
                                              "gate": {"accepted": False, "technical_failures": []}}}]
            calls = [{"id": f"{run_id}-call-{index}", "run_id": run_id, "phase": "queries", "status": "ok",
                      "raw_response": SECRET, "authorization": SECRET} for index in range(5 if is_repair else 3)]
            self.runs[run_id] = {"raw": raw, "calls": calls, "polls": 0, "number": number, "counted": True}
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.001)
            if number == self.ambiguous:
                raise httpx.ReadTimeout(SECRET, request=request)
            return httpx.Response(202, json={"id": run_id, "job_id": job_id, "status": "queued"})
        if path.endswith("/cancel"):
            self.cancellations.append(path.split("/")[-2])
            return httpx.Response(200, json={"status": "cancelled"})
        if path.startswith("/api/runs/"):
            run_id = path.split("/")[3]
            item = self.runs[run_id]
            if path.endswith("/trace"):
                return httpx.Response(200, json={"run": item["raw"], "calls": item["calls"]})
            item["polls"] += 1
            if item["polls"] < 2 or item["number"] == self.hanging:
                return httpx.Response(200, json={"id": run_id, "job_id": item["raw"]["job_id"], "status": "running"})
            if item["counted"]:
                self.active -= 1
                item["counted"] = False
            return httpx.Response(200, json=item["raw"])
        raise AssertionError(f"Unexpected fixture path: {path}")


async def fixture_load(data, config, api, *, cache: Literal["cold", "warm", "unspecified"] = "cold", fast_poll=False):
    plan = load_plan(data, config, cache=cache, store=SnapshotStore())
    if fast_poll:
        plan.update(poll_interval_seconds=0.001, polling_grace_seconds=0.0)
        plan = reseal(plan)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        return await run_api_load(plan, data, config, "http://fixture.local", client=client)


@pytest.mark.asyncio
async def test_four_concurrent_durable_api_journeys_retain_queue_repair_calls_and_shadow_status():
    config, data = configuration(), dataset(count=8)
    api = FixtureAPI(config)
    report = await fixture_load(data, config, api)
    assert report["complete"] is True
    assert api.peak == report["max_inflight_runs"] == report["concurrency"] == 4
    assert len(api.submissions) == len(report["samples"]) == 8
    assert {row["status"] for row in report["samples"]} == {"shadow"}
    assert report["summary"]["completed_without_release"] == 8
    assert {row["repair_attempted"] for row in report["samples"]} == {True, False}
    for row in report["samples"]:
        assert row["run_id"] and row["job_id"] and row["submission_outcome"] == "acknowledged"
        assert row["submission_retried"] is False and row["completion_without_release"] is True
        assert row["total_ms"] == max(row["client_observed_total_ms"], row["worker_reported_total_ms"])
        assert 0 <= row["queue_ms"] <= row["total_ms"]
        assert row["attempts"] == (5 if row["repair_attempted"] else 3)
    assert report["summary"]["strata"]["warm:repair"]["total_ms"]["p95"] is None
    assert report["fixture_only"] is True and report["quality_qualified"] is False
    assert SECRET not in json.dumps(report)
    assert _load_artifact_checks(report, source_report(data, config))["passed"] is False


@pytest.mark.asyncio
async def test_ambiguous_submission_is_not_retried_or_reported_as_known_zero_cost():
    config, data = configuration(), dataset(count=4)
    api = FixtureAPI(config, ambiguous=1)
    report = await fixture_load(data, config, api)
    assert report["complete"] is False and len(api.submissions) == len(report["samples"]) == 4
    failed = [row for row in report["samples"] if row["submission_outcome"] == "unknown"]
    assert len(failed) == 1 and failed[0]["run_id"] is None and failed[0]["attempts"] is None
    assert failed[0]["status"] == "failed" and failed[0]["submission_retried"] is False
    assert report["summary"]["unknown_attempt_rows"] == 1
    assert report["summary"]["sample_denominator"] == 4
    assert SECRET not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["failed_worker", "missing_queue", "configuration_drift"])
async def test_incomplete_api_measurements_and_failed_workers_cannot_pass(problem):
    config, data = configuration(), dataset(count=4)
    api = FixtureAPI(config, failure=2 if problem == "failed_worker" else None,
                     missing_queue=problem == "missing_queue", drift=problem == "configuration_drift")
    report = await fixture_load(data, config, api)
    assert report["complete"] is False and len(report["samples"]) == 4
    if problem == "configuration_drift":
        assert report["server_configuration_unchanged"] is False
    else:
        assert report["summary"]["technical_failures"] == 1
    if problem == "failed_worker":
        failed = next(row for row in report["samples"] if row["status"] == "failed")
        assert failed["repair_attempted"] is True and failed["attempts"] == 5
        assert report["summary"]["strata"]["cold:repair"]["count"] == 2
        assert report["summary"]["strata"]["cold:repair"]["failures"] == 1


@pytest.mark.asyncio
async def test_client_deadline_cancels_only_its_known_job_and_keeps_missing_measurements():
    config, data = configuration(), dataset(count=4)
    config.runtime.query_deadline_seconds = 0.05
    api = FixtureAPI(config, hanging=1)
    report = await fixture_load(data, config, api, fast_poll=True)
    assert report["complete"] is False and len(report["samples"]) == 4
    timed_out = next(row for row in report["samples"] if row["status"] == "timed_out")
    assert timed_out["cancellation_requested"] is True
    assert api.cancellations == [timed_out["job_id"]]
    assert timed_out["queue_ms"] is None and timed_out["attempts"] is None


@pytest.mark.asyncio
async def test_merge_preserves_both_cache_campaigns_and_recomputes_all_four_strata():
    config, data = configuration(), dataset(count=4)
    cold = await fixture_load(data, config, FixtureAPI(config, namespace="cold"), cache="cold")
    warm = await fixture_load(data, config, FixtureAPI(config, namespace="warm"), cache="warm")
    merged = merge_load_reports([cold, warm])
    assert merged["complete"] is True and len(merged["samples"]) == 8
    assert all(row["count"] == 2 and row["total_ms"]["p95"] is not None for row in merged["summary"]["strata"].values())
    assert merged["quality_qualified"] is False and merged["fixture_only"] is True
    assert len(merged["source_artifacts"]) == 2
    with pytest.raises(ExperimentError, match="cannot count twice"):
        merge_load_reports([cold, cold])
    modified = copy.deepcopy(warm)
    modified["samples"][0]["total_ms"] = 1
    with pytest.raises(ExperimentError, match="content hash"):
        merge_load_reports([cold, modified])
    changed_config = reseal({**warm, "config_hash": "different"}, "content_hash")
    with pytest.raises(ExperimentError, match="frozen identity"):
        merge_load_reports([cold, changed_config])


@pytest.mark.asyncio
async def test_live_calls_require_matching_budget_phase_before_api_or_hub_access():
    config, data = configuration(live=True), dataset(live=True)
    config.budgets.phase_max_estimated_cost_usd["evaluation"] = 0.0
    hub = FixedInputVerifier(config)
    with pytest.raises(ExperimentError, match="matching phase"):
        await run_repeatability(repeat_plan(data, config), data, Ledger(), hub, config)
    assert hub.calls == []
    config.budgets.phase_max_estimated_cost_usd["queries"] = 0.0
    plan = load_plan(data, config, store=SnapshotStore())
    api = FixtureAPI(config)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(ExperimentError, match="matching phase"):
            await run_api_load(plan, data, config, "http://fixture.local", client=client)
    assert api.status_reads == 0 and api.submissions == []


def test_cli_run_defaults_to_dry_run_with_no_http_or_store_access(tmp_path, monkeypatch, capsys):
    config, data = load_config(ROOT / "configs/mock.yaml"), dataset()
    source = source_report(data, config)
    data_path = tmp_path / "dataset.json"
    source_path = tmp_path / "source.json"
    plan_path = tmp_path / "repeat-plan.json"
    data_path.write_text(data.model_dump_json(), encoding="utf-8")
    save_experiment_file(source, source_path)
    ids = [part for case in data.questions for part in ("--question-id", case.id)]
    assert main(["--config", str(ROOT / "configs/mock.yaml"), "prepare-repeatability", str(data_path), str(source_path),
                 *ids, "--fixture-only", "--output", str(plan_path)]) == 0
    assert plan_path.exists()
    capsys.readouterr()

    def forbidden(*args, **kwargs):
        raise AssertionError("Dry run may not execute HTTP, models, or construct a Store")

    monkeypatch.setattr("evidence_lab.experiments.run_repeatability", forbidden)
    monkeypatch.setattr("evidence_lab.experiments.run_api_load", forbidden)
    monkeypatch.setattr("evidence_lab.storage.Store", forbidden)
    assert main(["--config", str(ROOT / "configs/mock.yaml"), "run-repeatability", str(data_path), str(plan_path)]) == 0
    estimate = json.loads(capsys.readouterr().out)
    assert estimate["dry_run"] is True and estimate["estimated_upper_bound_usd"] == 0
    assert main(["--config", str(ROOT / "configs/mock.yaml"), "run-repeatability", str(data_path), str(plan_path),
                 "--execute", "--output", str(plan_path)]) == 2
    output = capsys.readouterr().out
    assert '"error":' in output and SECRET not in output


def test_artifact_file_integrity_and_exclusive_save_preserve_earlier_results(tmp_path):
    path = tmp_path / "study.json"
    original = {"schema_version": 1, "complete": False, "failure": "budget_exhausted"}
    save_experiment_file(original, path)
    assert load_experiment_file(path) == original
    with pytest.raises(ExperimentError, match="new writable"):
        save_experiment_file({"complete": True}, path)
    assert load_experiment_file(path) == original
    malformed = tmp_path / "duplicate.json"
    malformed.write_text('{"complete":false,"complete":true}', encoding="utf-8")
    with pytest.raises(ExperimentError, match="safely"):
        load_experiment_file(malformed)
