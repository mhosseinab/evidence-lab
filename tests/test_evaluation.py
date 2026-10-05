"""Evaluation accounting and gate behavior with explicit, non-model fixtures."""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from rag_poc.config import Pricing, load_config
from rag_poc.domain import AnswerBlock, CallContext, CheckResult, Draft, EvidenceItem, EvidencePack, GLOBAL_IDS, VerificationResult, stable_hash
from rag_poc.evaluation import (
    AnswerReviews, DatasetError, EvaluationDataset, MUTATION_CATEGORIES,
    _artifact_binding, _fault_artifact_checks, _load_artifact_checks, _repeatability_artifact_checks,
    annotation_template, apply_annotations, bundled_dataset_path, compute_metrics, demo_documents,
    dry_run_estimate, evaluate_dataset, export_report, load_dataset, paired_family_difference,
    proportion, propose_mutation, qualify_policy, regrade_report, run_controlled_case,
    run_evaluation, run_question_case, wilson_interval, zero_event_upper_bound,
)
from rag_poc.providers import ProviderHub


class FixtureStore:
    """Only this unit test double avoids PostgreSQL; the app never uses it."""

    def __init__(self, *, prior_calls=(), cancelled=False):
        self.prior_calls = list(prior_calls)
        self.cancelled = cancelled

    def get_calls(self, run_id=None, prefix=None):
        return self.prior_calls

    def get_job(self, identifier):
        return {"id": identifier, "status": "cancelled" if self.cancelled else "running"}


@pytest.fixture
def config():
    return load_config("configs/mock.yaml")


@pytest.fixture
def dataset():
    return load_dataset(bundled_dataset_path())


@pytest.fixture
async def mock_report(config):
    hub = ProviderHub(config)
    try:
        return await evaluate_dataset(bundled_dataset_path(), FixtureStore(), hub, config, job_id="unit-demo")
    finally:
        await hub.aclose()


def make_draft(text="Premium members can return unopened items within 30 days.", citations=None):
    return Draft(blocks=[AnswerBlock(block_id="b1", text=text, citation_ids=citations or ["e-return-policy"])])


def verdict(draft, pack, round_id, *, rejected=(), score=1.0):
    checks = [CheckResult(id=block.block_id, kind="block_support",
                          support_status="not_supported" if block.block_id in rejected else "supported",
                          support_score=score)
              for block in draft.blocks]
    checks.extend(CheckResult(id=identifier, kind="global",
                              check_status="fail" if identifier in rejected else "pass", support_score=score)
                  for identifier in GLOBAL_IDS)
    return VerificationResult(checks=checks, answer_hash=draft.content_hash,
                              evidence_hash=pack.content_hash, round_id=round_id)


class ScriptedHub:
    def __init__(self, *, initial=None, repaired=None, callbacks=None):
        self.initial = initial or make_draft()
        self.repaired = repaired or self.initial
        self.callbacks = callbacks or [verdict]
        self.generations = []
        self.verifications = []

    async def generate(self, question, evidence, ctx, repair=None):
        ctx.consume_attempt()
        if repair is not None:
            assert set(repair) == {"failed_checks", "original_draft"}
            assert len(repair["failed_checks"]) == len(set(repair["failed_checks"]))
            expected = {block["block_id"] for block in repair["original_draft"]["blocks"]} | set(GLOBAL_IDS)
            assert all(item in expected for item in repair["failed_checks"])
        self.generations.append({"question": question, "evidence_hash": evidence.content_hash,
                                 "repair": copy.deepcopy(repair)})
        return (self.initial if repair is None else self.repaired).model_copy(deep=True)

    async def verify(self, question, draft, evidence, ctx, round_id="initial"):
        ctx.consume_attempt()
        self.verifications.append({"question": question, "draft": draft.model_dump(mode="json"),
                                   "evidence_hash": evidence.content_hash, "round_id": round_id})
        callback = self.callbacks[min(len(self.verifications) - 1, len(self.callbacks) - 1)]
        if isinstance(callback, Exception):
            raise callback
        return callback(draft, evidence, round_id)


async def execute_case(dataset, config, hub):
    return await run_question_case(dataset.questions[0], dataset, FixtureStore(), hub, config,
                                   CallContext.for_seconds("unit", "evaluation", 60, 10))


def test_wilson_zero_events_are_not_zero_uncertainty():
    interval = wilson_interval(0, 100)
    assert interval[0] == 0
    assert 0.036 < interval[1] < 0.038
    assert 0.029 < zero_event_upper_bound(100) < 0.030
    assert wilson_interval(0, 0) is None
    assert zero_event_upper_bound(0) is None
    assert proportion(0, 8, pending=8)["value"] is None
    assert proportion(0, 8, pending=8)["ci95_wilson"] is None
    for values in ((-1, 10), (2, 1), (True, 1)):
        with pytest.raises(ValueError):
            wilson_interval(*values)


def test_bootstrap_resamples_families_with_paired_case_counts():
    rows = [{"family_id": "large", "before": False, "after": True}] * 9
    rows += [{"family_id": "small", "before": True, "after": False}]
    result = paired_family_difference(rows, iterations=400)
    assert result["cases"] == 10 and result["families"] == 2
    assert result["delta"] == 0.8
    assert result["ci95_family_bootstrap"] == [-1.0, 1.0]
    assert result == paired_family_difference(rows, iterations=400)
    assert paired_family_difference(rows[:1])["ci95_family_bootstrap"] is None


def test_dataset_enforces_family_splits_and_exact_mutation_evidence(dataset):
    raw = dataset.model_dump(mode="json")
    raw["questions"][0]["split"] = "test"
    with pytest.raises(ValidationError, match="split"):
        EvaluationDataset.model_validate(raw)
    raw = dataset.model_dump(mode="json")
    raw["controlled"][1]["evidence_ids"] = ["e-dispatch-policy"]
    with pytest.raises(ValidationError, match="exact original evidence"):
        EvaluationDataset.model_validate(raw)
    raw = dataset.model_dump(mode="json")
    raw["evidence"][2]["text"] = raw["evidence"][0]["text"]
    with pytest.raises(ValidationError, match="cross source families"):
        EvaluationDataset.model_validate(raw)


async def test_paired_variants_generate_once_and_share_bound_input(dataset, config):
    hub = ScriptedHub()
    result = await execute_case(dataset, config, hub)
    assert len(hub.generations) == len(hub.verifications) == 1
    assert {row["answer_hash"] for row in result["variants"].values()} == {hub.initial.content_hash}
    assert all(row["status"] == "released" for row in result["variants"].values())
    assert hub.generations[0]["evidence_hash"] == hub.verifications[0]["evidence_hash"]


@pytest.mark.parametrize("violation", ["missing_citation", "duplicate_block", "extra_key"])
async def test_a_keeps_decodable_text_while_b_rejects_schema(dataset, config, mock_report, violation):
    decoded = make_draft().model_dump(mode="json")
    if violation == "missing_citation":
        decoded["blocks"][0].pop("citation_ids")
    elif violation == "duplicate_block":
        decoded["blocks"].append(copy.deepcopy(decoded["blocks"][0]))
    else:
        decoded["unrequested_field"] = "preserved for schema-gate rejection"

    class CandidateHub(ScriptedHub):
        async def generate_candidate(self, question, evidence, ctx):
            ctx.consume_attempt()
            self.generations.append({"candidate": True})
            return {"decoded": decoded, "text": "\n\n".join(block["text"] for block in decoded["blocks"]),
                    "candidate_hash": stable_hash(decoded)}

    hub = CandidateHub()
    result = await execute_case(dataset, config, hub)
    assert result["variants"]["A"]["released"]
    assert result["variants"]["A"]["answer_hash"] == stable_hash(decoded)
    assert result["initial_draft"] is None and result["initial_candidate"]["decoded"] == decoded
    assert all(result["variants"][name]["status"] == "failed" for name in ("B", "C", "D"))
    assert len(hub.generations) == 1 and not hub.verifications
    report = copy.deepcopy(mock_report)
    report["questions"][0] = result
    labels = AnswerReviews.model_validate({"dataset_hash": report["dataset_hash"], "answers": [{
        "question_id": result["id"], "answer_hash": stable_hash(decoded), "all_material_facts_reviewed": True,
        "substantive": True, "fully_supported": True, "correct_and_complete": True, "appropriate_response": True,
        "material_claims": [{"id": "unit-claim", "text": decoded["blocks"][0]["text"], "supported": True}],
        "review": {"status": "synthetic_fixture", "reviewer_ids": []},
    }]})
    metrics = compute_metrics(report, labels)
    assert metrics["natural_claims"]["initial"]["supported_retention"]["denominator"] == 1
    assert metrics["natural_claims"]["initial"]["supported_retention"]["numerator"] == 0


async def test_one_repair_uses_only_canonical_feedback_and_reverifies_unchanged_text(dataset, config):
    config.verification.score_threshold = 0.7
    def first(draft, pack, round_id):
        return verdict(draft, pack, round_id, rejected=("b1",), score=0.5)
    hub = ScriptedHub(callbacks=[first, verdict])
    result = await execute_case(dataset, config, hub)
    assert result["variants"]["C"]["status"] == "abstained"
    assert result["variants"]["D"]["status"] == "released"
    assert len(hub.generations) == len(hub.verifications) == 2
    assert hub.verifications[1]["round_id"] == "repair"
    assert hub.verifications[0]["draft"] == hub.verifications[1]["draft"]
    assert len({item["evidence_hash"] for item in hub.generations + hub.verifications}) == 1
    repair = hub.generations[1]["repair"]
    assert repair["original_draft"] == hub.initial.model_dump(mode="json")
    assert repair["failed_checks"].count("b1") == 1
    assert set(repair["failed_checks"]) == {"b1", *GLOBAL_IDS}
    assert "gold" not in json.dumps(repair)


def test_repair_canonicalization_preserves_colons_in_exact_check_ids():
    from rag_poc.evaluation import _repair_failure_ids
    draft = make_draft()
    draft.blocks[0].block_id = "section:claim"
    assert _repair_failure_ids(draft, ["section:claim", "section:claim:below_threshold"]) == ["section:claim"]


@pytest.mark.parametrize("violation", ["missing", "foreign", "round", "hash", "technical"])
async def test_technical_verification_failure_never_authorizes_content_repair(dataset, config, violation):
    def malformed(draft, pack, round_id):
        result = verdict(draft, pack, round_id)
        if violation == "missing":
            result.checks.pop()
        elif violation == "foreign":
            result.checks[0].id = "foreign"
        elif violation == "round":
            result.round_id = "stale"
        elif violation == "hash":
            result.answer_hash = "other-answer"
        else:
            result.execution_status = "provider_unavailable"
        return result
    hub = ScriptedHub(callbacks=[malformed])
    result = await execute_case(dataset, config, hub)
    assert result["variants"]["A"]["released"] and result["variants"]["B"]["released"]
    assert result["variants"]["C"]["status"] == result["variants"]["D"]["status"] == "verification_unavailable"
    assert len(hub.generations) == 1 and not result["repair_attempted"]


async def test_structural_gate_preserves_a_but_blocks_unknown_citations(dataset, config):
    hub = ScriptedHub(initial=make_draft(citations=["not-retrieved"]))
    result = await execute_case(dataset, config, hub)
    assert result["variants"]["A"]["released"]
    assert all(result["variants"][name]["status"] == "failed" for name in ("B", "C", "D"))
    assert not hub.verifications


async def test_controlled_original_claims_are_checked_without_repair(dataset, config):
    original = dataset.controlled[1]
    hub = ScriptedHub(callbacks=[lambda draft, pack, round_id: verdict(draft, pack, round_id, rejected=("claim",))])
    result = await run_controlled_case(original, dataset, hub, config, CallContext.for_seconds("unit", "evaluation"))
    assert not result["accepted"] and result["repair_disabled"]
    assert not hub.generations and len(hub.verifications) == 1
    assert hub.verifications[0]["draft"]["blocks"][0]["text"] == original.claim
    assert hub.verifications[0]["round_id"] == "controlled"


def test_demo_controlled_fixture_labels_cover_eight_mutation_families(mock_report, dataset):
    assert Counter(c.mutation_category for c in dataset.controlled if c.supported is False) == Counter(MUTATION_CATEGORIES)
    metrics = mock_report["metrics"]["controlled"]
    assert metrics["false_acceptance"]["numerator"] == 0
    assert metrics["false_acceptance"]["denominator"] == 8
    assert metrics["supported_retention"]["numerator"] == metrics["supported_retention"]["denominator"] == 8
    assert metrics["false_acceptance"]["ci95_wilson"][1] > 0.3
    assert all(c.review.status == "synthetic_fixture" and not c.review.reviewer_ids for c in dataset.controlled)
    assert mock_report["fixture_only"] and not mock_report["qualification"]["qualified"]


def test_failed_and_abstained_cases_stay_in_fixed_denominators(mock_report):
    report = copy.deepcopy(mock_report)
    answerable = [case for case in report["questions"] if case["expected_response"] == "answerable"]
    for index, case in enumerate(answerable):
        outcome = case["variants"]["D"]
        outcome.update(status="abstained" if index % 2 else "verification_unavailable", released=False)
    report["controlled"][0].update(accepted=False, execution_status="timeout")
    report["controlled"][1].update(accepted=False, execution_status="provider_unavailable")
    metrics = compute_metrics(report)
    assert metrics["variants"]["D"]["correct_and_complete"]["denominator"] == 4
    assert metrics["variants"]["D"]["correct_and_complete"]["numerator"] == 0
    assert metrics["variants"]["D"]["correct_and_complete"]["pending"] == 0
    assert metrics["controlled"]["supported_retention"]["denominator"] == 8
    assert metrics["controlled"]["supported_retention"]["numerator"] == 7
    assert metrics["controlled"]["false_acceptance"]["denominator"] == 8
    assert metrics["controlled"]["operational_completion"]["numerator"] == 14
    assert metrics["controlled"]["operational_completion"]["denominator"] == 16


def test_report_rejects_dropped_rows_and_changed_gold_metadata(mock_report):
    for change in ("drop", "question_label", "family", "control_label", "manifest"):
        report = copy.deepcopy(mock_report)
        if change == "drop":
            report["questions"].pop()
        elif change == "question_label":
            report["questions"][0]["expected_response"] = "missing_evidence"
        elif change == "family":
            report["questions"][0]["family_id"] = "independent-looking"
        elif change == "control_label":
            report["controlled"][0]["supported"] = False
        else:
            report["dataset_manifest"]["version"] = "changed"
        with pytest.raises(DatasetError):
            compute_metrics(report)


def test_unreviewed_outputs_are_unknown_not_perfect_answers(mock_report):
    metrics = mock_report["metrics"]
    assert metrics["variants"]["D"]["correct_and_complete"]["value"] is None
    assert metrics["variants"]["D"]["missing_evidence_handling"]["value"] is None
    assert metrics["variants"]["D"]["unsupported_release_rate"]["value"] is None
    assert metrics["variants"]["D"]["zero_error_upper95_if_independent"] is None
    assert metrics["required_evidence_coverage_at_8"]["status"] == "not_measured_inline_fixture"
    assert metrics["answer_review_coverage"]["reviewed_answers"] == 0


def test_unknown_usage_keeps_reservation_and_controlled_cost_stays_separate(mock_report):
    report = copy.deepcopy(mock_report)
    report["runtime_mode"] = "live"
    report["remote_attempts_observed"] = 3
    report["ledger_calls"] = [
        {"id": "q1", "run_id": "eval:unit-demo:q:q1", "actual_cost": 0.04, "estimated_cost": 0.06,
         "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        {"id": "q2", "run_id": "eval:unit-demo:q:q1", "actual_cost": None, "estimated_cost": 0.06, "usage": None},
        {"id": "c1", "run_id": "eval:unit-demo:c:c1", "actual_cost": 0.5, "estimated_cost": 0.5, "usage": None},
    ]
    costs = compute_metrics(report)["costs"]
    assert costs["question_calls"]["conservative_accounted_usd"] == pytest.approx(0.1)
    assert costs["controlled_calls"]["conservative_accounted_usd"] == 0.5
    assert costs["query_usd_per_declared_question"] == pytest.approx(0.1 / 7)
    assert costs["query_usd_per_D_correct_complete_answer"] is None
    assert costs["unknown_question_usage_calls"] == 1
    assert costs["question_token_fields"]["prompt_tokens"]["reported_sum"] == 10
    reviewed = regrade_report(report, AnswerReviews.model_validate(reviewed_template(report)))
    assert reviewed["metrics"]["costs"]["query_usd_per_D_correct_complete_answer"] == pytest.approx(0.1 / 4)


def reviewed_template(report):
    """Synthetic review input exclusively for validation tests, not gold assets."""
    template = annotation_template(report)
    packets = {(row["question_id"], row["answer_hash"]): row for row in template["review_packets"]}
    for answer in template["answers"]:
        packet = packets[(answer["question_id"], answer["answer_hash"])]
        substantive = bool(packet["blocks"])
        answer.update(all_material_facts_reviewed=True, substantive=substantive,
                      fully_supported=True, correct_and_complete=substantive, appropriate_response=True)
        answer["material_claims"] = [{"id": f"m{index}", "text": block["text"], "block_id": block["block_id"], "supported": True}
                                     for index, block in enumerate(packet["blocks"])]
        answer["review"] = {"status": "synthetic_fixture", "reviewer_ids": [], "notes": "Unit-test label, not a semantic finding."}
    return template


def test_annotation_packet_is_unfilled_blinded_and_hash_bound(mock_report):
    template = annotation_template(mock_report)
    assert all(answer["fully_supported"] is None for answer in template["answers"])
    assert all("variant" not in packet and "verification" not in packet and "gold" not in packet for packet in template["review_packets"])
    with pytest.raises(ValidationError):
        AnswerReviews.model_validate(template)
    valid = AnswerReviews.model_validate(reviewed_template(mock_report))
    assert regrade_report(mock_report, valid)["metrics"]["answer_review_coverage"]["reviewed_answers"] > 0
    for change in ("dataset", "answer", "span"):
        altered = valid.model_copy(deep=True)
        if change == "dataset":
            altered.dataset_hash = "different"
        elif change == "answer":
            altered.answers[0].answer_hash = "different"
        else:
            item = next(item for item in altered.answers if item.material_claims)
            item.material_claims[0].text = "This assertion was never in this answer."
        with pytest.raises(DatasetError):
            regrade_report(mock_report, altered)


async def test_restarted_evaluation_is_incomplete_without_replaying_calls(config):
    hub = ScriptedHub()
    job = {"id": "restarted", "attempts": 2, "payload": {"dataset": "demo"}}
    result = await run_evaluation(job, FixtureStore(), hub, config)
    assert result["status"] == "incomplete" and result["incomplete_reason"] == "interrupted_evaluation"
    assert len(result["questions"]) == 7 and len(result["controlled"]) == 16
    assert not hub.generations and not hub.verifications
    assert result["metrics"]["variants"]["D"]["live_completion"]["denominator"] == 7


async def test_cancelled_evaluation_retains_all_unattempted_cases(config):
    hub = ScriptedHub()
    job = {"id": "cancelled", "attempts": 1, "payload": {"dataset": "demo"}}
    result = await run_evaluation(job, FixtureStore(cancelled=True), hub, config)
    assert result["status"] == "incomplete" and not hub.generations
    assert all(row["execution_status"] == "cancelled" for row in result["controlled"])
    assert len(result["controlled"]) == 16


async def test_budget_exhaustion_retains_remaining_live_study_rows(tmp_path, monkeypatch, config, dataset):
    raw = dataset.model_dump(mode="json")
    raw["purpose"] = "operator_evaluation"
    for row in raw["families"] + raw["questions"] + raw["controlled"]:
        row["split"] = "test"
    for row in raw["questions"]:
        row["retrieval_mode"] = "corpus"
    path = tmp_path / "unit-operator.json"
    path.write_text(json.dumps(raw))
    operator_dataset = load_dataset(path)
    config.runtime.mode = "live"
    config.evaluation.max_remote_attempts = 1
    config.budgets.total_max_estimated_cost_usd = 100
    config.budgets.phase_max_estimated_cost_usd["evaluation"] = 100
    for profile in config.profiles.values():
        profile.pricing = Pricing(input_usd_per_million=1, output_usd_per_million=1, checked_on="2026-01-01")
    from rag_poc.evaluation import _fixture_pack
    async def retrieve(question, corpus_id, store, hub, cfg, ctx):
        ctx.consume_attempt()
        pack = _fixture_pack(operator_dataset.questions[0].evidence_ids, operator_dataset)
        return {key: pack for key in ("fused", "dense", "lexical")}
    monkeypatch.setattr("rag_poc.retrieval.retrieve_evidence_variants", retrieve)
    hub = ScriptedHub()
    report = await evaluate_dataset(path, FixtureStore(), hub, config)
    assert report["status"] == "incomplete" and report["incomplete_reason"] == "budget_exhausted"
    assert report["remote_attempts_observed"] == 1
    assert len(report["questions"]) == 7 and len(report["controlled"]) == 16
    assert sum(row["attempted"] for row in report["questions"]) == 1
    assert not hub.generations


async def test_evaluation_api_rejects_paths_and_live_fixture_calls(config):
    for payload in ({"dataset": "/etc/passwd"}, {"dataset": "demo", "path": "/etc/passwd"}):
        with pytest.raises(DatasetError):
            await run_evaluation({"id": "bad", "payload": payload}, FixtureStore(), ScriptedHub(), config)
    config.runtime.mode = "live"
    with pytest.raises(DatasetError, match="live"):
        await run_evaluation({"id": "bad", "payload": {"dataset": "demo"}}, FixtureStore(), ScriptedHub(), config)


def test_mutation_proposal_never_invents_an_adjudicated_label(dataset):
    original = next(row for row in dataset.controlled if row.id == "s-number_unit")
    proposal = propose_mutation(original, "€5", "€50", "number_unit", "proposed-unit-only")
    assert proposal["supported"] is None
    assert proposal["review"]["status"] == "unreviewed"
    assert proposal["evidence_ids"] == original.evidence_ids
    with pytest.raises(DatasetError):
        propose_mutation(original, "not present", "replacement", "number_unit", "new")


def test_dry_run_and_exports_preserve_review_and_fixture_limitations(tmp_path, mock_report, config):
    estimate = dry_run_estimate(bundled_dataset_path(), config)
    assert estimate["estimated_usd_upper_bound"] == 0
    assert estimate["logical_calls"]["query_embeddings"] == 0
    assert estimate["logical_calls"]["controlled_verification_batches"] == 16
    paths = export_report(mock_report, tmp_path)
    saved = json.loads(Path(paths["json"]).read_text())
    assert saved["dataset_hash"] == mock_report["dataset_hash"]
    assert len(Path(paths["questions_csv"]).read_text().splitlines()) == 29
    assert "Quality qualification: **unqualified**" in Path(paths["markdown"]).read_text()
    assert len(demo_documents(config)) == 4
    labels = tmp_path / "annotations.json"
    labels.write_text(json.dumps(reviewed_template(mock_report)))
    reapplied = apply_annotations(paths["json"], labels)
    assert reapplied["adjudication_applied"] and reapplied["answer_adjudications"]
    assert not reapplied["qualification"]["qualified"]


def artifact_base(report, kind):
    return {"schema_version": 1, "kind": kind, "artifact_id": "unit-artifact", "evaluation_id": report["id"],
            "dataset_hash": report["dataset_hash"], "semantic_fingerprint": report["identity"]["policy_hash"],
            "implementation_fingerprint": report["identity"]["implementation_fingerprint"], "complete": True,
            "generated_at": "2026-10-05T10:00:00+00:00", "runtime_mode": "live", "fixture_only": False}


def test_artifact_bindings_and_missing_native_fault_evidence_fail(mock_report):
    from rag_poc.evaluation import FAULT_AREAS
    artifact = artifact_base(mock_report, "fault_suite")
    artifact.update(native_postgres_verified=True, restore_verified=True, migration_revision="1",
                    postgres_version="unit", pgvector_version="unit", summary={"passed": 16, "failed": 0, "errors": 0, "skipped": 0},
                    checks={name: {"status": "passed", "test_ids": ["test_fixture.py::test_case"]} for name in FAULT_AREAS})
    assert _fault_artifact_checks(artifact, mock_report)["passed"]
    artifact["restore_verified"] = False
    assert not _fault_artifact_checks(artifact, mock_report)["passed"]
    artifact["dataset_hash"] = "different"
    assert "dataset_hash mismatch" in _artifact_binding(artifact, mock_report, "fault_suite")


def test_load_qualification_recomputes_queue_inclusive_repair_strata(mock_report):
    artifact = artifact_base(mock_report, "live_load")
    artifact.update(concurrency=4, verification_mode="shadow", samples=[])
    for index, (cache, repair) in enumerate((('cold', False), ('cold', True), ('warm', False), ('warm', True))):
        artifact["samples"].append({"question_id": mock_report["questions"][0]["id"], "run_id": str(index),
                                    "cache_state": cache, "repair_attempted": repair, "status": "shadow",
                                    "queue_ms": 1000, "total_ms": 30000 if repair else 15000, "attempts": 5 if repair else 3})
    assert _load_artifact_checks(artifact, mock_report)["passed"]
    artifact["samples"][0]["total_ms"] = 25000
    assert not _load_artifact_checks(artifact, mock_report)["passed"]
    artifact["samples"].pop()
    assert not _load_artifact_checks(artifact, mock_report)["passed"]


def test_fixture_report_cannot_qualify_with_missing_real_artifacts(mock_report, config):
    manifest = qualify_policy(mock_report, config, fault_artifact=None, load_artifact=None,
                              repeatability_artifact=None, policy_id="unit-only")
    assert not manifest["qualified"]
    assert {"live_mode", "real_evaluation_dataset", "mandatory_fault_suite", "live_concurrent_latency", "repeatability"}.issubset(manifest["unmet_requirements"])
    repeat = artifact_base(mock_report, "repeatability")
    assert not _repeatability_artifact_checks(repeat, mock_report)["passed"]


def simulated_qualified_study(config):
    """In-memory protocol simulation ONLY. Never saved/exported or called a study result.

    These invented labels and simulated-live flags exist to prove the qualifier
    has a reachable success branch. They are not real reviews or endpoint data.
    """
    from rag_poc.evaluation import FAULT_AREAS, _identity, _outcome
    config.runtime.mode = "live"
    gold_review = {"status": "adjudicated", "reviewer_ids": ["UNIT_TEST_R1", "UNIT_TEST_R2"],
                   "adjudicator_id": "UNIT_TEST_ADJUDICATOR", "reviewed_at": "2026-01-01T00:00:00+00:00",
                   "notes": "SIMULATED UNIT-TEST METADATA; not real human review."}
    families = [{"id": f"unit-family-{index}", "split": "test"} for index in range(20)]
    evidence = [{"id": f"unit-control-e{index}", "family_id": families[index]["id"],
                 "document_id": f"unit-d{index}", "version_id": f"unit-v{index}",
                 "title": "In-memory unit-test source", "text": f"The unit-test code of family {index} is {index}."}
                for index in range(20)]
    questions, rows, controlled, control_rows = [], [], [], []
    for index in range(200):
        family = index // 10
        kind = "answerable" if index < 140 else "missing_evidence" if index < 180 else "conflict"
        qid = f"unit-q{index}"
        text = f"The in-memory unit-test value of question {index} is {index}."
        facts = [{"id": "value", "description": "UNIT TEST ONLY", "evidence_alternatives": [{"quote": text}]}]
        if index < 40:
            qualification = f"The unit-test qualifier for question {index} applies."
            facts.append({"id": "qualifier", "description": "UNIT TEST ONLY", "evidence_alternatives": [{"quote": qualification}]})
            text += " " + qualification
        question = {"id": qid, "family_id": families[family]["id"], "split": "test", "question": f"UNIT TEST ONLY {index}",
                    "expected_response": kind, "retrieval_mode": "corpus", "corpus_id": "unit-corpus",
                    "gold": {"required_facts": facts if kind == "answerable" else [], "review": gold_review},
                    "tags": ["multi_evidence"] if index < 40 else []}
        questions.append(question)
        pack = EvidencePack(corpus_id="unit-corpus", space_id="unit-space", corpus_revision=1,
                            items=[EvidenceItem(id=f"unit-qe{index}", document_id=f"unit-d{family}", version_id=f"unit-v{family}",
                                                title="In-memory unit-test source", text=text)])
        draft = Draft(blocks=[AnswerBlock(block_id="b1", text=text, citation_ids=[f"unit-qe{index}"])]) if kind != "missing_evidence" else None
        outcome = _outcome("released", draft) if draft else _outcome("abstained")
        rows.append({"id": qid, "family_id": question["family_id"], "split": "test", "expected_response": kind,
                     "retrieval_mode": "corpus", "evidence": pack.model_dump(mode="json"),
                     "initial_draft": draft.model_dump(mode="json") if draft else None, "repair_draft": None,
                     "required_evidence_covered": kind == "answerable", "repair_attempted": False,
                     "timings_ms": {}, "variants": {variant: copy.deepcopy(outcome) for variant in ("A", "B", "C", "D")}})
        family = index % 20
        original = {"id": f"unit-s{index}", "family_id": families[family]["id"], "split": "test",
                    "claim": evidence[family]["text"], "evidence_ids": [evidence[family]["id"]], "supported": True, "review": gold_review}
        mutated = {**original, "id": f"unit-u{index}", "claim": f"UNIT TEST ONLY changed claim {index}", "supported": False,
                   "original_id": original["id"], "mutation_category": MUTATION_CATEGORIES[index % 8]}
        controlled.extend([original, mutated])
        for item in (original, mutated):
            control_rows.append({"id": item["id"], "family_id": item["family_id"], "split": "test", "supported": item["supported"],
                                 "original_id": item.get("original_id"), "mutation_category": item.get("mutation_category"),
                                 "accepted": item["supported"], "execution_status": "ok", "attempted": True})
    dataset = EvaluationDataset.model_validate({
        "schema_version": 1, "id": "IN_MEMORY_UNIT_TEST_ONLY", "version": "unit", "title": "Not a real evaluation",
        "purpose": "operator_evaluation", "frozen_at": "2026-01-01T00:00:00+00:00",
        "preselected_profile": config.role_name("verifier"), "families": families, "evidence": evidence,
        "questions": questions, "controlled": controlled,
    })
    report = {"id": "SIMULATED_UNIT_TEST_ONLY", "dataset_hash": dataset.content_hash,
              "dataset_manifest": dataset.model_dump(mode="json"), "selected_splits": ["test"],
              "runtime_mode": "live", "fixture_only": False, "primary_variant": "D", "identity": _identity(config),
              "started_at_epoch": 1_800_000_000, "score_threshold": None, "run_complete": True, "status": "completed",
              "questions": rows, "controlled": control_rows}
    labels = reviewed_template(report)
    for label in labels["answers"]:
        label["review"] = gold_review
    report = regrade_report(report, AnswerReviews.model_validate(labels))
    fault = artifact_base(report, "fault_suite")
    fault.update(native_postgres_verified=True, restore_verified=True, migration_revision="SIMULATED_UNIT_TEST",
                 postgres_version="SIMULATED_UNIT_TEST", pgvector_version="SIMULATED_UNIT_TEST",
                 summary={"passed": 16, "failed": 0, "errors": 0, "skipped": 0},
                 checks={name: {"status": "passed", "test_ids": ["tests/test_evaluation.py::test_positive_qualification_is_reachable_only_with_all_required_inputs"]}
                         for name in FAULT_AREAS})
    load = artifact_base(report, "live_load")
    load.update(concurrency=4, verification_mode="shadow", samples=[
        {"question_id": rows[index]["id"], "run_id": f"unit-load-{index}", "cache_state": cache,
         "repair_attempted": repaired, "status": "shadow", "queue_ms": 100, "total_ms": 30000 if repaired else 15000,
         "attempts": 5 if repaired else 3}
        for index, (cache, repaired) in enumerate((("cold", False), ("cold", True), ("warm", False), ("warm", True)))])
    repeat = artifact_base(report, "repeatability")
    repeat.update(measurement="verifier_repeatability", predeclared_at="2026-01-02T00:00:00+00:00",
                  started_at="2026-01-03T00:00:00+00:00", question_ids=[row["id"] for row in rows[:20]], repetitions=3,
                  runs=[{"question_id": row["id"], "repetition": number, "run_id": f"unit-repeat-{row['id']}-{number}",
                         "status": "ok", "decision": "accepted", "answer_hash": stable_hash(row["initial_draft"]),
                         "evidence_hash": stable_hash(row["evidence"])} for row in rows[:20] for number in (1, 2, 3)])
    return report, fault, load, repeat


def test_positive_qualification_is_reachable_only_with_all_required_inputs(config):
    report, fault, load, repeat = simulated_qualified_study(config)
    result = qualify_policy(report, config, fault_artifact=fault, load_artifact=load,
                            repeatability_artifact=repeat, policy_id="UNIT_TEST_ONLY_NEVER_EXPORT")
    assert result["qualified"], result["unmet_requirements"]
    assert result["human_reviewed"] and result["complete"]
    assert not report["qualification"]["qualified"]  # The paired report alone still lacks the external artifacts.
    changed = copy.deepcopy(fault)
    changed["implementation_fingerprint"] = "different-code"
    assert not qualify_policy(report, config, fault_artifact=changed, load_artifact=load,
                              repeatability_artifact=repeat, policy_id="UNIT_TEST_ONLY_NEVER_EXPORT")["qualified"]
    missing_reviews = copy.deepcopy(report)
    missing_reviews["answer_adjudications"] = None
    assert not qualify_policy(missing_reviews, config, fault_artifact=fault, load_artifact=load,
                              repeatability_artifact=repeat, policy_id="UNIT_TEST_ONLY_NEVER_EXPORT")["qualified"]
    dropped = copy.deepcopy(report)
    dropped["questions"].pop()
    with pytest.raises(DatasetError):
        qualify_policy(dropped, config, fault_artifact=fault, load_artifact=load,
                       repeatability_artifact=repeat, policy_id="UNIT_TEST_ONLY_NEVER_EXPORT")
