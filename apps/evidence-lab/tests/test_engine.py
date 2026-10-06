"""Observable release-gate behavior using explicit deterministic test doubles."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from evidence_lab import engine as engine_module
from evidence_lab.api import public_run
from evidence_lab.config import load_config
from evidence_lab.domain import AnswerBlock, CheckResult, Draft, EvidenceItem, EvidencePack, GLOBAL_IDS, ProviderError, VerificationResult
from evidence_lab.engine import QueryEngine
from evidence_lab.ingestion import text_hash
from evidence_lab.policy import evaluate_checks, policy_state, semantic_policy_fingerprint, structural_check


def evidence():
    text = "Employees receive 25 days of annual leave. The office opens at 09:00."
    return EvidencePack(corpus_id="default", space_id="fixture-space-64-v1", corpus_revision=1,
                        items=[EvidenceItem(id="c1", document_id="d1", version_id="v1", title="Policy", text=text,
                                            page=1, start=0, end=len(text), text_hash=text_hash(text))])


def draft(days=25):
    return Draft(blocks=[
        AnswerBlock(block_id="b1", text=f"Employees receive {days} days of annual leave.", citation_ids=["c1"]),
        AnswerBlock(block_id="b2", text="The office opens at 09:00.", citation_ids=["c1"]),
    ])


def verdict(draft, evidence, round_id="initial", rejected=()):
    checks = [CheckResult(id=block.block_id, kind="block_support",
                          support_status="not_supported" if block.block_id in rejected else "supported",
                          reason="insufficient_evidence" if block.block_id in rejected else None)
              for block in draft.blocks]
    checks.extend(CheckResult(id=identifier, kind="global", check_status="fail" if identifier in rejected else "pass",
                              reason="conflicting_evidence" if identifier in rejected else None)
                  for identifier in GLOBAL_IDS)
    return VerificationResult(checks=checks, answer_hash=draft.content_hash, evidence_hash=evidence.content_hash, round_id=round_id)


class ObservingStore:
    def __init__(self, *, age_seconds=0, prior_attempts=0):
        self.data = {"id": "run1", "question": "What are the leave entitlement and office opening time?", "corpus_id": "default", "status": "queued",
                     "created_at": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(), "answer": None, "blocks": []}
        self.history = []
        self.events = []
        self.prior_attempts = prior_attempts
        self.lease_token = "active-lease"

    def get_run(self, run_id):
        return deepcopy(self.data)

    def get_calls(self, run_id):
        return [{"id": str(index)} for index in range(self.prior_attempts)]

    def update_run(self, run_id, fields, lease=None):
        if lease is None or lease.get("token") != self.lease_token:
            raise ProviderError("lease_lost", "Fixture worker lease was replaced")
        self.data.update(deepcopy(fields))
        self.history.append(deepcopy(self.data))

    def append_event(self, run_id, event, lease=None):
        if lease is None or lease.get("token") != self.lease_token:
            raise ProviderError("lease_lost", "Fixture worker lease was replaced")
        self.events.append(deepcopy(event))


class ScriptedHub:
    def __init__(self, store, *, initial=None, repaired=None, results=None, after_verify=None):
        self.store = store
        self.initial = initial or draft()
        self.repaired = repaired or draft()
        self.results = results or [lambda answer, pack, round_id: verdict(answer, pack, round_id)]
        self.after_verify = after_verify
        self.generate_calls = []
        self.verify_calls = []

    async def generate(self, question, pack, ctx, repair=None):
        ctx.consume_attempt()
        if repair is not None and set(repair) != {"failed_checks", "original_draft"}:
            # Mirror the public provider contract at this boundary, so the engine
            # cannot accidentally work only with an overly permissive test fake.
            raise ProviderError("invalid_response", "Repair request does not match the provider contract")
        self.generate_calls.append({"question": question, "evidence": pack.model_dump(mode="json"), "evidence_hash": pack.content_hash, "repair": deepcopy(repair)})
        assert public_run(self.store.get_run("run1"))["answer"] is None
        return (self.repaired if repair is not None else self.initial).model_copy(deep=True)

    async def verify(self, question, answer, pack, ctx, round_id="initial"):
        ctx.consume_attempt()
        assert public_run(self.store.get_run("run1"))["answer"] is None
        self.verify_calls.append({"round_id": round_id, "draft": answer.model_dump(mode="json"), "answer_hash": answer.content_hash, "evidence_hash": pack.content_hash})
        response = self.results[min(len(self.verify_calls) - 1, len(self.results) - 1)]
        if isinstance(response, Exception):
            raise response
        result = response(answer, pack, round_id)
        if self.after_verify:
            self.after_verify(ctx)
        return result


def run_engine(monkeypatch, store, hub, *, pack=None, config=None):
    cfg = config or load_config("configs/mock.yaml")
    pack = pack if pack is not None else evidence()
    retrievals = []

    async def retrieve(question, corpus_id, store_arg, hub_arg, cfg_arg, ctx):
        ctx.consume_attempt()
        retrievals.append((question, corpus_id, pack.content_hash))
        return pack.model_copy(deep=True)

    monkeypatch.setattr(engine_module, "retrieve_evidence", retrieve)
    job = {"id": "job1", "token": "active-lease", "payload": {"run_id": "run1"}}
    result = asyncio.run(QueryEngine(store, hub, cfg).run(job))
    return result, retrievals


def test_supported_answer_is_published_only_after_complete_verification(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store)
    result, retrievals = run_engine(monkeypatch, store, hub)
    assert result["status"] == "answered"
    assert len(retrievals) == 1 and len(hub.verify_calls) == 1
    assert store.data["answer"] == draft().render()
    assert store.data["qualification"] == "fixture_only"
    assert all(public_run(row)["answer"] is None for row in store.history[:-1])
    assert all(not row.get("blocks") for row in store.history[:-1])
    assert [event["type"] for event in store.events].index("verification") > [event["type"] for event in store.events].index("draft")


def test_unsupported_answer_has_one_repair_and_rechecks_unchanged_blocks(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store, initial=draft(99), repaired=draft(25), results=[
        lambda answer, pack, round_id: verdict(answer, pack, round_id, rejected={"b1"}),
        lambda answer, pack, round_id: verdict(answer, pack, round_id),
    ])
    result, retrievals = run_engine(monkeypatch, store, hub)
    assert result["status"] == "answered"
    assert len(retrievals) == 1
    assert len(hub.generate_calls) == 2 and len(hub.verify_calls) == 2
    assert [call["round_id"] for call in hub.verify_calls] == ["initial", "repair"]
    assert hub.generate_calls[1]["repair"]["failed_checks"] == ["b1"]
    assert [block["block_id"] for block in hub.verify_calls[1]["draft"]["blocks"]] == ["b1", "b2"]
    assert hub.verify_calls[1]["draft"]["blocks"][1] == hub.verify_calls[0]["draft"]["blocks"][1]
    assert len({call["evidence_hash"] for call in hub.generate_calls + hub.verify_calls}) == 1
    assert "25 days" in store.data["answer"] and "99 days" not in store.data["answer"]


def test_second_semantic_failure_abstains_without_a_third_draft(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store, initial=draft(99), repaired=draft(99), results=[lambda answer, pack, round_id: verdict(answer, pack, round_id, rejected={"b1"})])
    result, _ = run_engine(monkeypatch, store, hub)
    assert result["status"] == "abstained"
    assert result["code"] == "unsupported_after_verification"
    assert len(hub.generate_calls) == 2 and len(hub.verify_calls) == 2
    assert store.data["answer"] is None and store.data["blocks"] == []


@pytest.mark.parametrize("field", ["answer_hash", "evidence_hash", "round_id"])
def test_stale_verification_metadata_never_releases_an_answer(monkeypatch, field):
    def stale(answer, pack, round_id):
        result = verdict(answer, pack, round_id)
        setattr(result, field, "stale-value")
        return result

    store = ObservingStore()
    hub = ScriptedHub(store, results=[stale])
    result, _ = run_engine(monkeypatch, store, hub)
    assert result["status"] == "verification_unavailable"
    assert store.data["answer"] is None and len(hub.generate_calls) == 1


def test_initial_round_verdict_cannot_approve_an_unchanged_repair(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store, initial=draft(99), repaired=draft(99), results=[
        lambda answer, pack, round_id: verdict(answer, pack, round_id, rejected={"b1"}),
        lambda answer, pack, round_id: verdict(answer, pack, "initial"),
    ])
    result, _ = run_engine(monkeypatch, store, hub)
    assert result["status"] == "verification_unavailable"
    assert store.data["answer"] is None


@pytest.mark.parametrize("fault", ["missing", "duplicate", "foreign", "wrong_kind"])
def test_incomplete_or_ambiguous_checks_are_technical_failures(monkeypatch, fault):
    def malformed(answer, pack, round_id):
        result = verdict(answer, pack, round_id)
        if fault == "missing":
            result.checks.pop()
        elif fault == "duplicate":
            result.checks.append(result.checks[0].model_copy(deep=True))
        elif fault == "foreign":
            result.checks[-1] = CheckResult(id="unrequested", kind="global", check_status="pass")
        else:
            result.checks[0] = CheckResult(id="b1", kind="global", check_status="pass")
        return result

    store = ObservingStore()
    hub = ScriptedHub(store, results=[malformed])
    result, _ = run_engine(monkeypatch, store, hub)
    assert result["status"] == "verification_unavailable"
    assert len(hub.generate_calls) == 1 and store.data["answer"] is None


def test_verifier_outage_does_not_trigger_repair_or_semantic_abstention(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store, results=[ProviderError("provider_unavailable", "Fixture verifier unavailable")])
    result, _ = run_engine(monkeypatch, store, hub)
    assert result["status"] == "verification_unavailable"
    assert result["code"] == "provider_unavailable"
    assert len(hub.generate_calls) == 1 and store.data["message"] is None


def test_empty_retrieval_abstains_without_generation(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store)
    pack = evidence()
    pack.items = []
    result, _ = run_engine(monkeypatch, store, hub, pack=pack)
    assert result["status"] == "abstained" and result["code"] == "insufficient_evidence"
    assert hub.generate_calls == hub.verify_calls == []


def test_query_deadline_includes_time_waiting_in_queue(monkeypatch):
    store = ObservingStore(age_seconds=120)
    hub = ScriptedHub(store)
    result, retrievals = run_engine(monkeypatch, store, hub)
    assert result["status"] == "timed_out" and result["code"] == "timeout"
    assert retrievals == [] and hub.generate_calls == []


def test_deadline_is_rechecked_before_publishing_a_completed_verdict(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store, after_verify=lambda ctx: setattr(ctx, "deadline", ctx.deadline - 1000))
    result, _ = run_engine(monkeypatch, store, hub)
    assert result["status"] == "timed_out"
    assert store.data["answer"] is None


def test_restart_cannot_reset_existing_outbound_attempt_count(monkeypatch):
    store = ObservingStore(prior_attempts=10)
    hub = ScriptedHub(store)
    result, retrievals = run_engine(monkeypatch, store, hub)
    assert result["status"] == "failed" and result["code"] == "budget_exhausted"
    assert retrievals == [] and hub.generate_calls == []


def test_worker_that_loses_lease_cannot_publish_terminal_answer(monkeypatch):
    store = ObservingStore()
    hub = ScriptedHub(store, after_verify=lambda ctx: setattr(store, "lease_token", "new-worker"))
    with pytest.raises(ProviderError) as caught:
        run_engine(monkeypatch, store, hub)
    assert caught.value.status == "lease_lost"
    assert store.data["answer"] is None


def test_shadow_policy_keeps_draft_only_in_operator_diagnostics(monkeypatch):
    cfg = load_config("configs/mock.yaml")
    cfg.verification.mode = "shadow"
    store = ObservingStore()
    hub = ScriptedHub(store)
    result, _ = run_engine(monkeypatch, store, hub, config=cfg)
    assert result["status"] == "shadow"
    assert store.data["answer"] is None and store.data["blocks"] == []
    assert any(event["type"] == "draft" for event in store.events)
    assert "draft" not in public_run(store.data)


def test_verified_mode_releases_live_answer_without_claiming_qualification(monkeypatch):
    cfg = load_config("configs/mock.yaml")
    cfg.runtime.mode = "live"
    cfg.verification.mode = "verified"
    state = policy_state(cfg)
    assert state["release_allowed"] is True and state["qualified"] is False
    store = ObservingStore()
    hub = ScriptedHub(store)
    result, _ = run_engine(monkeypatch, store, hub, config=cfg)
    assert result["status"] == "answered"
    assert store.data["qualification"] == "verified"
    assert public_run(store.data)["answer"] == draft().render()
    assert all(public_run(row)["answer"] is None for row in store.history[:-1])


@pytest.mark.parametrize("failure", ["unsupported", "incomplete", "generation", "verification"])
def test_verified_mode_never_releases_failed_checks_or_provider_errors(monkeypatch, failure):
    cfg = load_config("configs/mock.yaml")
    cfg.runtime.mode = "live"
    cfg.verification.mode = "verified"
    store = ObservingStore()
    hub = ScriptedHub(store)
    if failure == "unsupported":
        hub.results = [lambda answer, pack, round_id: verdict(answer, pack, round_id, rejected=("b1",))]
    elif failure == "incomplete":
        def incomplete(answer, pack, round_id):
            result = verdict(answer, pack, round_id)
            result.checks.pop()
            return result
        hub.results = [incomplete]
    elif failure == "verification":
        hub.results = [ProviderError("invalid_response", "Invalid verification")]
    else:
        async def invalid_generation(*args, **kwargs):
            raise ProviderError("invalid_response", "Provider answer failed schema validation")
        monkeypatch.setattr(hub, "generate", invalid_generation)
    result, _ = run_engine(monkeypatch, store, hub, config=cfg)
    assert result["status"] != "answered"
    assert public_run(store.data)["answer"] is None
    assert all(public_run(row)["answer"] is None for row in store.history)


def test_unknown_citation_fails_before_any_semantic_call(monkeypatch):
    invalid = draft()
    invalid.blocks[0].citation_ids = ["foreign"]
    store = ObservingStore()
    hub = ScriptedHub(store, initial=invalid)
    result, _ = run_engine(monkeypatch, store, hub)
    assert result["status"] == "failed" and result["code"] == "invalid_response"
    assert hub.verify_calls == [] and len(hub.generate_calls) == 1


def test_scoreless_verdict_does_not_gain_fabricated_confidence():
    pack, answer = evidence(), draft()
    result = evaluate_checks(answer, pack, verdict(answer, pack), threshold=0.9)
    assert result["accepted"] is False
    assert any("missing_native_score" in failure for failure in result["technical_failures"])


def test_repair_profile_change_invalidates_semantic_policy_fingerprint():
    cfg = load_config("configs/mock.yaml")
    before = semantic_policy_fingerprint(cfg)
    cfg.profiles["alternate-repair"] = cfg.role_profile("generator").model_copy(deep=True)
    cfg.profiles["alternate-repair"].model = "different-repair-model"
    cfg.roles.repair_generator = "alternate-repair"
    assert semantic_policy_fingerprint(cfg) != before


def test_repair_byte_bound_change_invalidates_semantic_policy_fingerprint():
    cfg = load_config("configs/mock.yaml")
    before = semantic_policy_fingerprint(cfg)
    cfg.verification.max_repair_bytes += 1024
    assert semantic_policy_fingerprint(cfg) != before


def test_embedding_execution_change_invalidates_qualified_policy(tmp_path):
    from evidence_lab.evaluation import implementation_fingerprint
    from evidence_lab.retrieval import space_manifest

    cfg = load_config("configs/mock.yaml")
    cfg.runtime.mode = "live"
    cfg.verification.policy_id = "heldout-policy"
    cfg.verification.policy_path = str(tmp_path / "policy.json")
    artifact = {
        "policy_id": cfg.verification.policy_id,
        "semantic_fingerprint": semantic_policy_fingerprint(cfg),
        "implementation_fingerprint": implementation_fingerprint(),
        "qualified": True, "runtime_mode": "live", "human_reviewed": True,
        "evaluation_id": "heldout-evaluation", "primary_variant": "D", "complete": True,
    }
    (tmp_path / "policy.json").write_text(json.dumps(artifact))
    assert policy_state(cfg)["release_allowed"] is True
    original_space = space_manifest(cfg)["fingerprint"]

    # Explicit live mode has the same execution contract as the default.
    cfg.runtime.embedding_mode = "live"
    assert policy_state(cfg)["release_allowed"] is True
    cfg.runtime.embedding_mode = "mock"
    assert space_manifest(cfg)["fingerprint"] != original_space
    state = policy_state(cfg)
    assert state["release_allowed"] is False and state["state"] == "unqualified"


@pytest.mark.parametrize("artifact", [[], None, "not an object", 12])
def test_nonobject_policy_artifact_is_unqualified_instead_of_crashing(tmp_path, artifact):
    cfg = load_config("configs/mock.yaml")
    cfg.runtime.mode = "live"
    cfg.verification.policy_id = "heldout-policy"
    cfg.verification.policy_path = str(tmp_path / "policy.json")
    (tmp_path / "policy.json").write_text(json.dumps(artifact))
    state = policy_state(cfg)
    assert state["release_allowed"] is False and state["state"] == "unqualified"


def test_structural_gate_rejects_ambiguous_evidence_ids():
    pack = evidence()
    pack.items.append(pack.items[0].model_copy(deep=True))
    result = structural_check(draft(), pack)
    assert result["accepted"] is False


def test_public_view_strips_stale_answer_and_diagnostics_during_reprocessing():
    view = public_run({"id": "run1", "status": "verifying", "answer": "previous answer", "blocks": [{"text": "previous answer"}], "draft": "private draft", "events": [{"draft": "private draft"}]})
    assert view["answer"] is None and view["blocks"] == []
    assert "draft" not in view and "events" not in view
