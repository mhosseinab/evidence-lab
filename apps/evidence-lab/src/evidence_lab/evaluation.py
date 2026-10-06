"""Paired, evidence-bound evaluation. Synthetic fixtures never qualify a model.

Provider calls go through ProviderHub. The durable worker owns the evaluation job
lease and final result; no model, tokenizer, or production storage fallback lives
in this module. Answer adjudication is a separate, hash-bound operator artifact.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import hashlib
import json
import math
import random
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal, TypeGuard

from pydantic import Field, ValidationError, model_validator

from evidence_lab.domain import (
    AnswerBlock, CallContext, Contract, Draft, EvidenceItem, EvidencePack,
    ProviderError, VerificationResult, stable_hash, strict_json,
)
from evidence_lab.providers.transport import output_reservation

EVALUATION_VERSION = "paired-abcd-v1"
VARIANTS = ("A", "B", "C", "D")
MUTATION_CATEGORIES = (
    "negation", "number_unit", "entity", "date", "condition",
    "scope_quantifier", "unsupported_addition", "misleading_combination",
)
SPLITS = Literal["development", "test", "demo"]
INSUFFICIENT_MESSAGE = "The supplied evidence is insufficient to provide a verified answer."
TECHNICAL_MESSAGE = "Verification could not be completed. No verified answer is available."
PROJECT_DATA = Path(__file__).resolve().parents[4] / "data"
FAULT_AREAS = (
    "configuration", "protocol_boundary", "authentication_network", "embeddings", "generation",
    "verification_coverage", "grounding", "repair", "input_budget", "adversarial_data",
    "source_index_state", "snapshot_lease_races", "retrieval", "trace_retention",
    "evaluation_accounting", "recovery",
)


class DatasetError(ValueError):
    """A safe, operator-facing dataset/annotation contract error."""


class Review(Contract):
    status: Literal["unreviewed", "synthetic_fixture", "single_reviewed", "adjudicated"] = "unreviewed"
    reviewer_ids: list[str] = Field(default_factory=list)
    adjudicator_id: str | None = None
    reviewed_at: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def distinct_reviewers(self):
        ids = self.reviewer_ids
        if len(ids) != len(set(ids)) or any(not item.strip() for item in ids):
            raise ValueError("Reviewer IDs must be nonempty and distinct")
        if self.status == "adjudicated" and (
            len(ids) < 2 or not self.adjudicator_id or not self.reviewed_at
        ):
            raise ValueError("Adjudicated labels require two reviewers, adjudicator, and date")
        if self.status == "single_reviewed" and len(ids) != 1:
            raise ValueError("Single-reviewed labels require one reviewer")
        return self

    @property
    def independently_reviewed(self) -> bool:
        return self.status == "adjudicated"


class SourceFamily(Contract):
    id: str = Field(min_length=1)
    split: SPLITS


class SourceDocument(Contract):
    id: str = Field(min_length=1)
    family_id: str
    title: str
    path: str
    media_type: str = "text/markdown"


class FixtureEvidence(EvidenceItem):
    family_id: str

    def as_item(self) -> EvidenceItem:
        values = self.model_dump(exclude={"family_id"})
        values["text_hash"] = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        values["end"] = self.end or self.start + len(self.text)
        return EvidenceItem.model_validate(values)


class EvidenceAlternative(Contract):
    quote: str = Field(min_length=1)
    document_id: str | None = None
    version_id: str | None = None


class RequiredFact(Contract):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_alternatives: list[EvidenceAlternative] = Field(default_factory=list)


class QuestionGold(Contract):
    required_facts: list[RequiredFact] = Field(default_factory=list)
    necessary_qualifiers: list[str] = Field(default_factory=list)
    prohibited_conclusions: list[str] = Field(default_factory=list)
    review: Review = Field(default_factory=Review)


class QuestionCase(Contract):
    id: str = Field(min_length=1)
    family_id: str
    split: SPLITS
    question: str = Field(min_length=1)
    expected_response: Literal["answerable", "missing_evidence", "conflict"]
    corpus_id: str = "default"
    retrieval_mode: Literal["corpus", "inline_fixture"] = "corpus"
    evidence_ids: list[str] = Field(default_factory=list)
    gold: QuestionGold = Field(default_factory=QuestionGold)
    tags: list[str] = Field(default_factory=list)


class ControlledCase(Contract):
    id: str = Field(min_length=1)
    family_id: str
    split: SPLITS
    claim: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    supported: bool | None = None
    original_id: str | None = None
    mutation_category: str | None = None
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def mutation_contract(self):
        if self.mutation_category is not None and self.mutation_category not in MUTATION_CATEGORIES:
            raise ValueError("Unknown mutation category")
        if self.original_id is not None and self.mutation_category is None:
            raise ValueError("Mutation needs an error category")
        if self.mutation_category is not None and self.original_id is None:
            raise ValueError("A categorized mutation must reference its supported original")
        return self


class DevelopmentSelection(Contract):
    report_id: str = Field(min_length=1)
    dataset_hash: str = Field(min_length=1)
    selected_at: str = Field(min_length=1)


class EvaluationDataset(Contract):
    schema_version: int = 1
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    title: str
    purpose: Literal["synthetic_fixture", "operator_evaluation"]
    language: str = "en"
    notes: str = ""
    frozen_at: str | None = None
    preselected_profile: str | None = None
    preselected_score_threshold: float | None = Field(default=None, ge=0, le=1)
    development_selection: DevelopmentSelection | None = None
    primary_variant: Literal["D"] = "D"
    families: list[SourceFamily] = Field(min_length=1)
    documents: list[SourceDocument] = Field(default_factory=list)
    evidence: list[FixtureEvidence] = Field(default_factory=list)
    questions: list[QuestionCase] = Field(default_factory=list)
    controlled: list[ControlledCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_splits_and_pairs(self):
        if self.schema_version != 1:
            raise ValueError("Unsupported dataset schema version")
        for collection in (self.families, self.documents, self.evidence, self.questions, self.controlled):
            ids = [item.id for item in collection]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate IDs in dataset collection")
        if not self.questions and not self.controlled:
            raise ValueError("Dataset needs question or controlled cases")
        families = {f.id: f.split for f in self.families}
        evidence = {e.id: e for e in self.evidence}
        for source in [*self.documents, *self.evidence]:
            if source.family_id not in families:
                raise ValueError("Unknown source family")
        # Reusing identical evidence as supposedly independent families is leakage.
        hashes: dict[str, str] = {}
        for item in self.evidence:
            normalized = " ".join(item.text.split())
            digest = hashlib.sha256(normalized.encode()).hexdigest()
            if digest in hashes and hashes[digest] != item.family_id:
                raise ValueError("Identical evidence cannot cross source families")
            hashes[digest] = item.family_id
        for case in [*self.questions, *self.controlled]:
            if families.get(case.family_id) != case.split:
                raise ValueError("Case split must match its source family")
            if len(case.evidence_ids) != len(set(case.evidence_ids)):
                raise ValueError("Duplicate evidence reference")
            for eid in case.evidence_ids:
                if eid not in evidence:
                    raise ValueError("Unknown evidence reference")
                if evidence[eid].family_id != case.family_id:
                    raise ValueError("Evidence must belong to the case source family")
        for case in self.questions:
            if case.retrieval_mode == "inline_fixture" and self.purpose != "synthetic_fixture":
                raise ValueError("Inline question evidence is restricted to synthetic fixtures")
        controlled = {c.id: c for c in self.controlled}
        for case in self.controlled:
            if case.original_id:
                original = controlled.get(case.original_id)
                if original is None or original.id == case.id or original.original_id is not None:
                    raise ValueError("Mutation must reference a supported original")
                if original.supported is not True:
                    raise ValueError("Mutation original must have a supported label")
                if (case.family_id, case.split) != (original.family_id, original.split):
                    raise ValueError("Matched originals and mutations must share family/split")
                if case.evidence_ids != original.evidence_ids:
                    raise ValueError("Claim mutations must retain the exact original evidence")
                if case.claim == original.claim:
                    raise ValueError("Mutation must change the claim")
        return self

    @property
    def content_hash(self) -> str:
        return stable_hash(self)


class MaterialClaimReview(Contract):
    id: str
    text: str = Field(min_length=1)
    block_id: str | None = None
    supported: bool


class AnswerReview(Contract):
    question_id: str
    answer_hash: str
    all_material_facts_reviewed: bool = False
    substantive: bool
    fully_supported: bool
    correct_and_complete: bool
    appropriate_response: bool
    material_claims: list[MaterialClaimReview] = Field(default_factory=list)
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def consistent_labels(self):
        if len({c.id for c in self.material_claims}) != len(self.material_claims):
            raise ValueError("Duplicate reviewed material-claim ID")
        if self.fully_supported and any(not c.supported for c in self.material_claims):
            raise ValueError("Fully-supported answer cannot contain unsupported material claims")
        if self.correct_and_complete and (not self.fully_supported or not self.substantive):
            raise ValueError("Correct-complete answer must be supported and substantive")
        if self.substantive and self.all_material_facts_reviewed and not self.material_claims:
            raise ValueError("A fully reviewed substantive answer needs material-claim annotations")
        return self


class AnswerReviews(Contract):
    schema_version: int = 1
    dataset_hash: str
    answers: list[AnswerReview]
    review_packets: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_answers(self):
        if self.schema_version != 1:
            raise ValueError("Unsupported answer-review schema")
        ids = [(a.question_id, a.answer_hash) for a in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate question/answer adjudication")
        return self


def load_dataset(path: str | Path) -> EvaluationDataset:
    try:
        file = Path(path)
        if file.stat().st_size > 20 * 1024 * 1024:
            raise DatasetError("Dataset manifest exceeds 20 MiB")
        return EvaluationDataset.model_validate(strict_json(file.read_text(encoding="utf-8")))
    except DatasetError:
        raise
    except (OSError, ValueError) as exc:
        # Validation errors may include operator data. Do not put them in API errors.
        raise DatasetError("Cannot read dataset or its manifest violates the evaluation schema") from exc


def _read_reviews(path: str | Path, dataset_hash: str) -> AnswerReviews:
    try:
        result = AnswerReviews.model_validate(strict_json(Path(path).read_text(encoding="utf-8")))
        if result.dataset_hash != dataset_hash:
            raise DatasetError("Answer reviews belong to a different frozen dataset")
        return result
    except DatasetError:
        raise
    except (OSError, ValueError) as exc:
        raise DatasetError("Cannot read answer adjudications or their schema is invalid") from exc


def wilson_interval(successes: int, denominator: int, confidence: float = 0.95) -> list[float] | None:
    """Two-sided Wilson score interval; no data returns null, never zero certainty."""
    if (type(successes) is not int or type(denominator) is not int
            or not 0 <= successes <= denominator or not 0 < confidence < 1):
        raise ValueError("Invalid proportion counts or confidence")
    if denominator == 0:
        return None
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = successes / denominator
    scale = 1 + z * z / denominator
    midpoint = (p + z * z / (2 * denominator)) / scale
    half = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator**2)) / scale
    return [max(0.0, midpoint - half), min(1.0, midpoint + half)]


def proportion(successes: int, denominator: int, *, pending: int = 0) -> dict[str, Any]:
    interval = wilson_interval(successes, denominator)
    if type(pending) is not int or pending < 0 or pending > denominator - successes:
        raise ValueError("Invalid pending count")
    return {
        "numerator": successes, "denominator": denominator, "pending": pending,
        "value": successes / denominator if denominator and not pending else None,
        "observed_lower_bound": successes / denominator if denominator else None,
        "ci95_wilson": interval if not pending else None,
        "status": "incomplete_review" if pending else "measured" if denominator else "not_measured",
        "interval_assumption": "item independence; source-family correlations are reported separately",
    }


def zero_event_upper_bound(denominator: int, confidence: float = 0.95) -> float | None:
    if type(denominator) is not int or denominator < 0 or not 0 < confidence < 1:
        raise ValueError("Invalid zero-event bound arguments")
    return -math.expm1(math.log1p(-confidence) / denominator) if denominator else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo = math.floor(index)
    hi = math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def paired_family_difference(
    rows: list[dict[str, Any]], *, iterations: int = 2000, seed: int = 17,
) -> dict[str, Any]:
    """Rows have family_id, before, after. Resample entire source families."""
    if iterations < 100:
        raise ValueError("Use at least 100 bootstrap repetitions")
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if type(row["before"]) is not bool or type(row["after"]) is not bool:
            raise ValueError("Paired outcomes must be boolean and fully reviewed")
        groups[row["family_id"]].append(int(row["after"]) - int(row["before"]))
    count = len(rows)
    result = {
        "cases": count, "families": len(groups), "delta":
        sum(sum(v) for v in groups.values()) / count if count else None,
        "ci95_family_bootstrap": None, "iterations": iterations, "seed": seed,
        "method": "percentile bootstrap of whole source families; paired case-weighted rate difference",
        "limitation": "few families and observed zero events do not establish zero future risk",
    }
    if len(groups) < 2:
        result["status"] = "insufficient_families"
        return result
    rng = random.Random(seed)
    family_ids = sorted(groups)
    values = []
    summaries = {k: (sum(v), len(v)) for k, v in groups.items()}
    for _ in range(iterations):
        selected = [summaries[rng.choice(family_ids)] for _ in family_ids]
        values.append(sum(x[0] for x in selected) / sum(x[1] for x in selected))
    result["ci95_family_bootstrap"] = [_quantile(values, 0.025), _quantile(values, 0.975)]
    result["status"] = "measured"
    return result


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        obj = obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
        if obj is None:
            return default
    return obj


def _runtime_mode(config: Any) -> str:
    return _get(config, "runtime", "mode", default="mock")


def implementation_fingerprint() -> str:
    """Bind reports to application code and the browser's release display logic."""
    root = Path(__file__).resolve().parent
    files = [path for path in root.rglob("*") if path.is_file()
             and path.suffix in (".py", ".js", ".html", ".css", ".txt")
             and "__pycache__" not in path.parts]
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(files)}
    dashboard = Path.cwd() / "apps/dashboard/dist"
    for path in sorted(dashboard.rglob("*")):
        if path.is_file():
            hashes[f"dashboard/{path.relative_to(dashboard)}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return stable_hash(hashes)


def _identity(config: Any) -> dict[str, Any]:
    profiles = {}
    for role in ("embeddings", "generator", "verifier", "repair_generator"):
        try:
            profile = config.role_profile(role)
            profiles[role] = {
                "profile": config.role_name(role), "model": profile.model,
                "protocol": getattr(profile, "protocol", None),
                "semantic_revision": getattr(profile, "semantic_revision", None),
            }
        except (AttributeError, KeyError, ValueError):
            continue
    try:
        from evidence_lab.policy import semantic_policy_fingerprint
        policy_hash = semantic_policy_fingerprint(config)
    except (ImportError, AttributeError, KeyError, ValueError):
        policy_hash = None
    return {
        "config_hash": config.fingerprint() if hasattr(config, "fingerprint") else None,
        "implementation_fingerprint": implementation_fingerprint(),
        "policy_hash": policy_hash, "profiles": profiles,
        "unpinned_model_aliases": [role for role, p in profiles.items() if not p["semantic_revision"]],
    }


def _fixture_pack(ids: list[str], dataset: EvaluationDataset) -> EvidencePack:
    mapping = {e.id: e for e in dataset.evidence}
    return EvidencePack(
        corpus_id="synthetic-fixture", space_id="inline-no-embedding",
        corpus_revision=1, items=[mapping[eid].as_item() for eid in ids],
        counting_method="fixture_no_retrieval_performed",
    )


def _outcome(status: str, draft: Draft | None = None, *, reason: str | None = None) -> dict[str, Any]:
    text = draft.render() if status == "released" and draft else (
        INSUFFICIENT_MESSAGE if status == "abstained" else TECHNICAL_MESSAGE
    )
    return {
        "status": status, "released": status == "released",
        "final_text": text,
        "answer_hash": draft.content_hash if status == "released" and draft else stable_hash({"text": text}),
        "draft_hash": draft.content_hash if draft else None,
        "reason": reason,
    }


def _error_status(exc: BaseException, verification: bool = False) -> tuple[str, str]:
    code = getattr(exc, "status", None)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or code == "timeout":
        return "timed_out", "timeout"
    if code == "cancelled" or isinstance(exc, asyncio.CancelledError):
        return "cancelled", "cancelled"
    allowed = {"budget_exhausted", "over_budget", "provider_unavailable", "invalid_response", "incomplete_coverage"}
    code = code if code in allowed else "provider_error"
    return ("verification_unavailable" if verification else "failed"), code


async def _call(awaitable: Any, ctx: CallContext) -> Any:
    try:
        remaining = ctx.remaining()
    except Exception:
        # A coroutine may already have been constructed by the caller.
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise
    async with asyncio.timeout(remaining):
        return await awaitable


def _safe_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {"id", "call_id", "run_id", "phase", "profile", "model", "protocol", "status", "usage",
               "estimated_cost", "actual_cost", "estimated_cost_usd", "actual_cost_usd", "charged_cost", "cost_known",
               "duration_ms", "latency_seconds", "attempt"}
    return [{k: v for k, v in call.items() if k in allowed} for call in calls]


def _coverage(case: QuestionCase, pack: EvidencePack) -> bool | None:
    if case.retrieval_mode != "corpus" or case.expected_response != "answerable":
        return None
    if not case.gold.required_facts or any(not f.evidence_alternatives for f in case.gold.required_facts):
        return None
    return all(any(
        alternative.quote in item.text
        and (alternative.document_id is None or alternative.document_id == item.document_id)
        and (alternative.version_id is None or alternative.version_id == item.version_id)
        for alternative in fact.evidence_alternatives for item in pack.items[:8]
    ) for fact in case.gold.required_facts)


def _completed_gate(draft: Draft, pack: EvidencePack, result: VerificationResult, config: Any, round_id: str) -> dict[str, Any]:
    from evidence_lab.policy import evaluate_checks
    if result.round_id != round_id:
        raise ProviderError("incomplete_coverage", "Verification round mismatch")
    gate = evaluate_checks(draft, pack, result, threshold=_get(config, "verification", "score_threshold"))
    if result.execution_status != "ok":
        raise ProviderError(result.execution_status, "Verification did not complete")
    if gate.get("technical_failures"):
        raise ProviderError("incomplete_coverage", "Verification failed its technical contract")
    # The policy validator distinguishes invalid bindings/coverage from a valid
    # semantic rejection. Reject malformed coverage before authorizing repair.
    from evidence_lab.domain import GLOBAL_IDS
    expected = {b.block_id for b in draft.blocks} | set(GLOBAL_IDS)
    ids = [check.id for check in result.checks]
    if (len(ids) != len(set(ids)) or set(ids) != expected
            or any(c.kind != ("global" if c.id in GLOBAL_IDS else "block_support") for c in result.checks)
            or result.answer_hash != draft.content_hash or result.evidence_hash != pack.content_hash):
        raise ProviderError("incomplete_coverage", "Verification binding or coverage mismatch")
    return gate


def _repair_failure_ids(draft: Draft, failures: list[str]) -> list[str]:
    from evidence_lab.domain import GLOBAL_IDS
    expected = {block.block_id for block in draft.blocks} | set(GLOBAL_IDS)
    result = []
    for item in failures:
        # A valid block ID can itself contain a colon. Prefer exact membership
        # before removing the policy's known descriptive suffix.
        identifier = item if item in expected else item.removesuffix(":below_threshold")
        if identifier not in expected:
            raise ProviderError("incomplete_coverage", "Repair feedback contains an unknown check ID")
        if identifier not in result:
            result.append(identifier)
    return result


async def run_question_case(
    case: QuestionCase, dataset: EvaluationDataset, store: Any, hub: Any,
    config: Any, ctx: CallContext,
) -> dict[str, Any]:
    """One initial retrieval/draft, B/C/D reuse it; only D can repair once."""
    from evidence_lab.policy import structural_check
    started = time.monotonic()
    record: dict[str, Any] = {
        "id": case.id, "family_id": case.family_id, "split": case.split,
        "expected_response": case.expected_response,
        "retrieval_mode": case.retrieval_mode, "required_evidence_covered": None,
        "initial_draft": None, "repair_draft": None, "evidence": None,
        "initial_verification": None, "repair_verification": None,
        "repair_attempted": False, "variants": {}, "timings_ms": {},
        "review_only": True,
    }
    phase = "retrieval"
    try:
        stage = time.monotonic()
        if case.retrieval_mode == "inline_fixture":
            pack = _fixture_pack(case.evidence_ids, dataset)
        else:
            from evidence_lab.retrieval import retrieve_evidence_variants
            packs = await _call(retrieve_evidence_variants(case.question, case.corpus_id, store, hub, config, ctx), ctx)
            pack = packs["fused"]
            record["retrieval_baselines"] = {
                name: {"required_evidence_covered": _coverage(case, value),
                       "evidence_hash": value.content_hash, "selected_ids": [item.id for item in value.items]}
                for name, value in packs.items()
            }
        record["timings_ms"]["retrieval"] = (time.monotonic() - stage) * 1000
        record["evidence"] = pack.model_dump(mode="json")
        record["evidence_hash"] = pack.content_hash
        record["required_evidence_covered"] = _coverage(case, pack)
        if not pack.items:
            record["variants"] = {variant: _outcome("abstained", reason="no_usable_evidence") for variant in VARIANTS}
            return record
        phase = "generation"
        stage = time.monotonic()
        if callable(getattr(hub, "generate_candidate", None)):
            candidate = await _call(hub.generate_candidate(case.question, pack, ctx), ctx)
            record["initial_candidate"] = candidate
            record["candidate_decode_coverage"] = candidate.get("decode_coverage", "decoded_blocks_text")
            try:
                draft = Draft.model_validate(candidate["decoded"])
            except (ValidationError, ValueError, TypeError):
                record["timings_ms"]["generation"] = (time.monotonic() - stage) * 1000
                record["variants"]["A"] = {
                    "status": "released", "released": True, "final_text": candidate["text"],
                    "answer_hash": candidate["candidate_hash"], "draft_hash": None,
                    "reason": "decodable_candidate_before_schema_gate",
                }
                record["structural_check"] = {"accepted": False, "failures": ["draft_schema"]}
                for variant in ("B", "C", "D"):
                    record["variants"][variant] = _outcome("failed", reason="structural_rejection")
                return record
        else:
            # Typed test doubles/older integrations cannot estimate the schema
            # gate's effect; this limitation is explicit in each case record.
            draft = await _call(hub.generate(case.question, pack, ctx), ctx)
            record["candidate_decode_coverage"] = "typed_only_schema_effect_not_measured"
        record["timings_ms"]["generation"] = (time.monotonic() - stage) * 1000
        record["initial_draft"] = draft.model_dump(mode="json")
        record["initial_draft_hash"] = draft.content_hash
        record["variants"]["A"] = _outcome("released", draft)
        structure = structural_check(draft, pack)
        record["structural_check"] = structure
        if not structure["accepted"]:
            for variant in ("B", "C", "D"):
                record["variants"][variant] = _outcome("failed", reason="structural_rejection")
            return record
        record["variants"]["B"] = _outcome("released", draft)
        phase = "initial_verification"
        stage = time.monotonic()
        result = await _call(hub.verify(case.question, draft, pack, ctx, round_id="initial"), ctx)
        record["timings_ms"]["initial_verification"] = (time.monotonic() - stage) * 1000
        record["initial_verification"] = result.model_dump(mode="json", exclude={"raw"})
        gate = _completed_gate(draft, pack, result, config, "initial")
        record["initial_gate"] = gate
        if gate["accepted"]:
            for variant in ("C", "D"):
                record["variants"][variant] = _outcome("released", draft)
            return record
        record["variants"]["C"] = _outcome("abstained", reason="semantic_rejection")
        if _get(config, "verification", "max_content_repairs", default=1) < 1:
            record["variants"]["D"] = copy.deepcopy(record["variants"]["C"])
            return record
        # The only repair context is production-visible verifier feedback.
        # Gold, expected_response, required_facts and annotations never enter hub.
        record["repair_attempted"] = True
        phase = "repair_generation"
        stage = time.monotonic()
        repaired = await _call(hub.generate(case.question, pack, ctx, repair={
            "failed_checks": gate["failed_check_ids"] if "failed_check_ids" in gate else _repair_failure_ids(draft, gate["failures"]),
            "original_draft": draft.model_dump(mode="json"),
        }), ctx)
        record["timings_ms"]["repair_generation"] = (time.monotonic() - stage) * 1000
        record["repair_draft"] = repaired.model_dump(mode="json")
        record["repair_draft_hash"] = repaired.content_hash
        repaired_structure = structural_check(repaired, pack)
        if not repaired_structure["accepted"]:
            record["variants"]["D"] = _outcome("failed", reason="repair_structural_rejection")
            return record
        phase = "repair_verification"
        stage = time.monotonic()
        repaired_result = await _call(hub.verify(case.question, repaired, pack, ctx, round_id="repair"), ctx)
        record["timings_ms"]["repair_verification"] = (time.monotonic() - stage) * 1000
        record["repair_verification"] = repaired_result.model_dump(mode="json", exclude={"raw"})
        repaired_gate = _completed_gate(repaired, pack, repaired_result, config, "repair")
        record["repair_gate"] = repaired_gate
        record["variants"]["D"] = _outcome(
            "released" if repaired_gate["accepted"] else "abstained",
            repaired if repaired_gate["accepted"] else None, reason=None if repaired_gate["accepted"] else "semantic_rejection_after_repair",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status, code = _error_status(exc, verification="verification" in phase)
        if phase == "retrieval" and case.retrieval_mode == "corpus" and case.expected_response == "answerable":
            record["required_evidence_covered"] = False
            record["retrieval_baselines"] = {name: {"required_evidence_covered": False, "error_code": code}
                                             for name in ("fused", "dense", "lexical")}
        for variant in VARIANTS:
            record["variants"].setdefault(variant, _outcome(status, reason=code))
        record["failure_phase"] = phase
        record["error_code"] = code
    finally:
        record["timings_ms"]["total_execution"] = (time.monotonic() - started) * 1000
        record["attempts_used"] = ctx.attempts_used
        record["calls"] = _safe_calls(ctx.calls)
        record["timing_note"] = "paired replay shares work; not an independent live latency benchmark"
    return record


async def run_controlled_case(
    case: ControlledCase, dataset: EvaluationDataset, hub: Any, config: Any, ctx: CallContext,
) -> dict[str, Any]:
    """Verify the original supplied claim only. No generation or repair is allowed."""
    started = time.monotonic()
    record: dict[str, Any] = {
        "id": case.id, "family_id": case.family_id, "split": case.split,
        "supported": case.supported, "original_id": case.original_id,
        "mutation_category": case.mutation_category, "accepted": False,
        "execution_status": "not_attempted", "repair_disabled": True,
        "label_review": case.review.model_dump(mode="json"),
    }
    try:
        pack = _fixture_pack(case.evidence_ids, dataset)
        draft = Draft(blocks=[AnswerBlock(block_id="claim", text=case.claim, citation_ids=case.evidence_ids)])
        result = await _call(hub.verify(
            "Assess whether the supplied claim is supported by the supplied evidence.",
            draft, pack, ctx, round_id="controlled",
        ), ctx)
        gate = _completed_gate(draft, pack, result, config, "controlled")
        record.update({
            "accepted": gate["accepted"], "execution_status": "ok",
            "claim_hash": draft.content_hash, "evidence_hash": pack.content_hash,
            "verification": result.model_dump(mode="json", exclude={"raw"}),
        })
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _, code = _error_status(exc, verification=True)
        record["execution_status"] = code
    finally:
        record["attempts_used"] = ctx.attempts_used
        record["calls"] = _safe_calls(ctx.calls)
        record["execution_ms"] = (time.monotonic() - started) * 1000
    return record


def _review_targets(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Unique initial/repaired/final text, without revealing variant or verdict."""
    targets = {}
    questions = {q["id"]: q for q in report.get("dataset_manifest", {}).get("questions", [])}
    for case in report.get("questions", []):
        qid = case["id"]
        base = {
            "question_id": qid, "question": questions.get(qid, {}).get("question", ""),
            "evidence": case.get("evidence"),
        }
        for stage in ("initial_draft", "repair_draft"):
            if case.get(stage):
                draft = Draft.model_validate(case[stage])
                targets[(qid, draft.content_hash)] = {
                    **base, "answer_hash": draft.content_hash, "text": draft.render(),
                    "blocks": draft.model_dump(mode="json")["blocks"],
                }
        for outcome in case.get("variants", {}).values():
            key = (qid, outcome["answer_hash"])
            if key not in targets:
                targets[key] = {**base, "answer_hash": outcome["answer_hash"],
                                "text": outcome["final_text"], "blocks": []}
    return targets


def annotation_template(report: dict[str, Any]) -> dict[str, Any]:
    """Null labels are deliberate: generated templates are not completed gold."""
    targets = _review_targets(report)
    return {
        "schema_version": 1, "dataset_hash": report["dataset_hash"],
        "answers": [{
            "question_id": qid, "answer_hash": answer_hash,
            "all_material_facts_reviewed": False, "substantive": None,
            "fully_supported": None, "correct_and_complete": None,
            "appropriate_response": None, "material_claims": [],
            "review": {"status": "unreviewed", "reviewer_ids": [], "adjudicator_id": None,
                       "reviewed_at": None, "notes": "Fill only after independent review and adjudication."},
        } for qid, answer_hash in sorted(targets)],
        "review_packets": [targets[key] for key in sorted(targets)],
    }


def _validated_reviews(report: dict[str, Any], annotations: AnswerReviews | None) -> dict[tuple[str, str], AnswerReview]:
    if annotations is None:
        return {}
    if annotations.dataset_hash != report["dataset_hash"]:
        raise DatasetError("Answer reviews do not match this frozen dataset")
    targets = _review_targets(report)
    result = {}
    for item in annotations.answers:
        key = (item.question_id, item.answer_hash)
        target = targets.get(key)
        if target is None:
            raise DatasetError("Answer adjudication does not match an output of this run")
        blocks = {b["block_id"]: b["text"] for b in target["blocks"]}
        for claim in item.material_claims:
            text = blocks.get(claim.block_id) if claim.block_id else target["text"]
            if text is None or claim.text not in text:
                raise DatasetError("Reviewed claim is not a verbatim span in its bound answer")
        if item.all_material_facts_reviewed and item.review.status != "unreviewed":
            result[key] = item
    return result


def _case_truth(case: dict[str, Any], variant: str, reviews: dict, field: str) -> bool | None:
    output = case["variants"][variant]
    if output["status"] not in ("released", "abstained"):
        return False
    if field == "correct_and_complete" and not output["released"]:
        return False
    review = reviews.get((case["id"], output["answer_hash"]))
    return getattr(review, field) if review else None


def _reviewed_rate(cases: list[dict], variant: str, reviews: dict, field: str) -> dict[str, Any]:
    values = [_case_truth(case, variant, reviews, field) for case in cases]
    return proportion(sum(value is True for value in values), len(cases), pending=sum(value is None for value in values))


def _natural_claim_rates(report: dict[str, Any], reviews: dict) -> dict[str, Any]:
    rounds = {}
    threshold = report.get("score_threshold")
    for phase in ("initial", "repair"):
        supported = unsupported = retained = falsely_accepted = 0
        pending_drafts = 0
        unmapped_claims = 0
        for case in report.get("questions", []):
            raw_draft = case.get(f"{phase}_draft")
            raw_candidate = case.get("initial_candidate") if phase == "initial" else None
            if not raw_draft and not raw_candidate:
                continue
            draft = Draft.model_validate(raw_draft) if raw_draft else None
            answer_hash = draft.content_hash if draft else case["variants"]["A"]["answer_hash"]
            review = reviews.get((case["id"], answer_hash))
            if review is None:
                pending_drafts += 1
                continue
            raw_result = case.get(f"{phase}_verification") or {}
            gate = case.get(f"{phase}_gate")
            decision_valid = raw_result.get("execution_status") == "ok" and isinstance(gate, dict) and not gate.get("technical_failures")
            checks = {c["id"]: c for c in raw_result.get("checks", [])}
            for claim in review.material_claims:
                check = checks.get(claim.block_id, {})
                if claim.block_id is None and draft is not None:
                    unmapped_claims += 1
                accepted = bool(decision_valid and check.get("support_status") == "supported")
                if threshold is not None:
                    score = check.get("support_score")
                    accepted = accepted and score is not None and score >= threshold
                if claim.supported:
                    supported += 1
                    retained += int(accepted)
                else:
                    unsupported += 1
                    falsely_accepted += int(accepted)
        rounds[phase] = {
            "false_acceptance": proportion(falsely_accepted, unsupported),
            "supported_retention": proportion(retained, supported),
            "unreviewed_drafts": pending_drafts, "unmapped_claims": unmapped_claims,
            "complete_review": pending_drafts == 0 and unmapped_claims == 0,
            "definition": "material claims accepted by their block check; whole-answer release is reported separately",
        }
        if pending_drafts or unmapped_claims:
            for name in ("false_acceptance", "supported_retention"):
                rounds[phase][name].update(value=None, ci95_wilson=None, status="incomplete_review")
                rounds[phase][name]["denominator_note"] = "Reviewed material claims only; remaining draft/claim counts are unknown until annotation is complete."
    return rounds


def _controlled_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    supported = [r for r in rows if r.get("supported") is True]
    unsupported = [r for r in rows if r.get("supported") is False]
    original_lookup = {r["id"]: r for r in rows}
    pairs = []
    for mutation in unsupported:
        original = original_lookup.get(mutation.get("original_id"))
        if original is not None:
            pairs.append({"family_id": mutation["family_id"],
                          "before": bool(original["accepted"]), "after": bool(mutation["accepted"])})
    by_error = {}
    for name in sorted({category for r in unsupported if (category := r.get("mutation_category"))}):
        group = [r for r in unsupported if r.get("mutation_category") == name]
        by_error[name] = proportion(sum(bool(r["accepted"]) for r in group), len(group))
    return {
        "false_acceptance": proportion(sum(bool(r["accepted"]) for r in unsupported), len(unsupported)),
        "supported_retention": proportion(sum(bool(r["accepted"]) for r in supported), len(supported)),
        "operational_completion": proportion(sum(r["execution_status"] == "ok" for r in rows), len(rows)),
        "unknown_gold_labels": sum(r.get("supported") is None for r in rows),
        "execution_statuses": dict(Counter(r["execution_status"] for r in rows)),
        "false_acceptance_by_mutation": by_error,
        "families": dict(Counter(r["family_id"] for r in rows)),
        "matched_acceptance_difference": paired_family_difference(pairs),
        "repair_disabled": True,
        "interpretation": "balanced challenge of supplied claims; never a natural error prevalence estimate",
    }


def compute_metrics(report: dict[str, Any], annotations: AnswerReviews | None = None) -> dict[str, Any]:
    """All declared cases must be present, including not-attempted failure rows."""
    dataset = EvaluationDataset.model_validate(report["dataset_manifest"])
    if dataset.content_hash != report["dataset_hash"]:
        raise DatasetError("The frozen dataset does not match its report hash")
    selected = report["selected_splits"]
    if (not selected or len(selected) != len(set(selected))
            or any(item not in ("development", "test", "demo") for item in selected)):
        raise DatasetError("The report has an invalid split selection")
    expected_questions = {q.id for q in dataset.questions if q.split in selected}
    expected_controlled = {q.id for q in dataset.controlled if q.split in selected}
    cases = report.get("questions", [])
    controlled = report.get("controlled", [])
    if (len(cases) != len(expected_questions) or {c["id"] for c in cases} != expected_questions
            or len(controlled) != len(expected_controlled) or {c["id"] for c in controlled} != expected_controlled):
        raise DatasetError("Evaluation output dropped or duplicated a declared case")
    if any(set(c["variants"]) != set(VARIANTS) for c in cases):
        raise DatasetError("Every question requires all four paired variants")
    question_manifest = {q.id: q for q in dataset.questions}
    controlled_manifest = {c.id: c for c in dataset.controlled}
    for row in cases:
        original = question_manifest[row["id"]]
        if any(row.get(name) != getattr(original, name)
               for name in ("family_id", "split", "expected_response", "retrieval_mode")):
            raise DatasetError("Question output metadata differs from the frozen manifest")
    for row in controlled:
        original = controlled_manifest[row["id"]]
        if any(row.get(name) != getattr(original, name)
               for name in ("family_id", "split", "supported", "original_id", "mutation_category")):
            raise DatasetError("Controlled output metadata differs from the frozen manifest")
    reviews = _validated_reviews(report, annotations)
    answerable = [c for c in cases if c["expected_response"] == "answerable"]
    missing = [c for c in cases if c["expected_response"] == "missing_evidence"]
    conflicting = [c for c in cases if c["expected_response"] == "conflict"]
    coverage_values = [c.get("required_evidence_covered") for c in answerable]
    retrieval = proportion(sum(v is True for v in coverage_values), len(answerable), pending=sum(v is None for v in coverage_values))
    if any(c["retrieval_mode"] == "inline_fixture" for c in answerable):
        retrieval["status"] = "not_measured_inline_fixture"
        retrieval["value"] = None
        retrieval["ci95_wilson"] = None
    variants = {}
    for variant in VARIANTS:
        releases = [c for c in cases if c["variants"][variant]["released"]]
        release_reviews = [reviews.get((c["id"], c["variants"][variant]["answer_hash"])) for c in releases]
        final_reviews = [reviews.get((c["id"], c["variants"][variant]["answer_hash"])) for c in cases]
        unsafe_releases = sum(r is not None and not r.fully_supported for r in release_reviews)
        pending_releases = sum(r is None for r in release_reviews)
        variants[variant] = {
            "correct_and_complete": _reviewed_rate(answerable, variant, reviews, "correct_and_complete"),
            "missing_evidence_handling": _reviewed_rate(missing, variant, reviews, "appropriate_response"),
            "conflict_handling": _reviewed_rate(conflicting, variant, reviews, "appropriate_response"),
            "live_completion": proportion(sum(c["variants"][variant]["status"] in ("released", "abstained") for c in cases), len(cases)),
            "unsupported_release_rate": proportion(unsafe_releases, len(releases), pending=pending_releases),
            "released_count": len(releases),
            "reviewed_substantive_releases": sum(r is not None and r.substantive for r in release_reviews),
            "final_response_audit": {
                "responses": len(cases), "reviewed": sum(r is not None for r in final_reviews),
                "pending": sum(r is None for r in final_reviews),
                "materially_unsupported": sum(r is not None and not r.fully_supported for r in final_reviews),
            },
            "zero_error_upper95_if_independent": zero_event_upper_bound(len(releases)) if releases and not unsafe_releases and not pending_releases else None,
            "statuses": dict(Counter(c["variants"][variant]["status"] for c in cases)),
        }
    paired = {}
    for before, after in (("A", "B"), ("B", "C"), ("C", "D")):
        rows = []
        pending = 0
        for case in answerable:
            left = _case_truth(case, before, reviews, "correct_and_complete")
            right = _case_truth(case, after, reviews, "correct_and_complete")
            if left is None or right is None:
                pending += 1
            else:
                rows.append({"family_id": case["family_id"], "before": left, "after": right})
        if pending:
            paired[f"{before}_to_{after}"] = {
                "status": "incomplete_review", "pending": pending, "cases": len(answerable),
                "delta": None, "ci95_family_bootstrap": None,
            }
        else:
            paired[f"{before}_to_{after}"] = paired_family_difference(rows)
    timings = {}
    for repaired in (False, True):
        rows = [c for c in cases if c.get("repair_attempted", False) == repaired]
        values = [float(c["timings_ms"]["total_execution"]) for c in rows if c.get("timings_ms", {}).get("total_execution") is not None]
        timings["repair_attempted" if repaired else "no_repair_attempted"] = {
            "cases": len(rows), "timed_cases": len(values),
            "p50_execution_ms": _quantile(values, 0.5), "p95_execution_ms": _quantile(values, 0.95),
            "status": "fixture_only" if report["runtime_mode"] == "mock" else "paired_execution_not_live_slo",
        }
    branches = {}
    for branch in ("dense", "lexical", "fused"):
        values = [c.get("retrieval_baselines", {}).get(branch, {}).get("required_evidence_covered") for c in answerable]
        branches[branch] = proportion(sum(v is True for v in values), len(values), pending=sum(v is None for v in values))
    targets = _review_targets(report)
    return {
        "questions": len(cases), "question_types": dict(Counter(c["expected_response"] for c in cases)),
        "source_families": dict(Counter(c["family_id"] for c in cases)),
        "required_evidence_coverage_at_8": retrieval, "retrieval_baselines": branches,
        "variants": variants, "paired_correct_complete_differences": paired,
        "controlled": _controlled_metrics(controlled), "natural_claims": _natural_claim_rates(report, reviews),
        "costs": _evaluation_costs(report, variants["D"]["correct_and_complete"]),
        "timing": timings,
        "answer_review_coverage": {
            "distinct_answers": len(targets), "reviewed_answers": len(reviews),
            "independently_reviewed_answers": sum(r.review.independently_reviewed for r in reviews.values()),
        },
        "annotation_provenance": "operator-supplied adjudications, hash-bound to exact outputs; reviewer identities are not authenticated by this application",
    }


def qualification_report(report: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    """Report evidence requirements explicitly; replay cannot certify a live SLO."""
    dataset = EvaluationDataset.model_validate(report["dataset_manifest"])
    selected = report["selected_splits"]
    questions = [q for q in dataset.questions if q.split in selected]
    controlled = [c for c in dataset.controlled if c.split in selected]
    d = metrics["variants"]["D"]
    a = metrics["variants"]["A"]
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, passed: bool, detail: str):
        checks[name] = {"passed": bool(passed), "detail": detail}

    check("live_mode", report["runtime_mode"] == "live", "Mock results are deterministic plumbing fixtures.")
    check("real_evaluation_dataset", dataset.purpose == "operator_evaluation", "Synthetic demo labels are not independent human-reviewed gold.")
    check("frozen_primary_profile", bool(dataset.frozen_at and dataset.preselected_profile and dataset.preselected_profile == report.get("identity", {}).get("profiles", {}).get("verifier", {}).get("profile")), "Freeze and preselect the verifier profile on development data before held-out execution.")
    check("complete_run", report.get("run_complete") is True, "Budget exhaustion, cancellation, or interrupted work cannot qualify a partial study.")
    frozen_at = _timestamp(dataset.frozen_at)
    check("frozen_before_execution", frozen_at is not None and frozen_at <= report.get("started_at_epoch", 0),
          "The held-out dataset and selected policy must be frozen before the first study call.")
    snapshots: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for row in report["questions"]:
        pack = row.get("evidence")
        if pack and row.get("retrieval_mode") == "corpus":
            snapshots[pack["corpus_id"]].add((pack["space_id"], pack["corpus_revision"]))
    check("frozen_corpus_snapshots", bool(snapshots) and all(len(values) == 1 for values in snapshots.values()),
          "Every question for a corpus must observe the same frozen embedding space and active-source revision.")
    types = metrics["question_types"]
    check("held_out_sample", selected == ["test"] and types == {"answerable": 140, "missing_evidence": 40, "conflict": 20} and len(metrics["source_families"]) == 20,
          "Require exactly 140 answerable, 40 missing-evidence, and 20 conflict questions from 20 held-out families.")
    check("multi_evidence_cases", sum(q.expected_response == "answerable" and "multi_evidence" in q.tags
          and len(q.gold.required_facts) >= 2
          and len({alternative.quote for fact in q.gold.required_facts for alternative in fact.evidence_alternatives}) >= 2
          for q in questions) >= 40,
          "At least 40 held-out answerable cases need a multi-evidence label and at least two required facts with distinct gold spans.")
    errors = Counter(c.mutation_category for c in controlled if c.supported is False)
    original_ids = {c.id for c in controlled if c.supported is True and c.original_id is None and c.mutation_category is None}
    mutation_pairs = [c.original_id for c in controlled if c.supported is False]
    check("controlled_sample", len(controlled) == 400 and len(original_ids) == 200 and len(mutation_pairs) == 200
          and len(set(mutation_pairs)) == 200 and set(mutation_pairs) == original_ids
          and all(errors[name] == 25 for name in MUTATION_CATEGORIES),
          "Require a one-to-one match of 200 supported originals and 200 reviewed unsupported mutations, 25 per error category.")
    check("question_and_controlled_gold_reviewed", all(q.gold.review.independently_reviewed for q in questions)
          and all(c.review.independently_reviewed for c in controlled) and bool(questions and controlled),
          "Two independent reviewers must adjudicate question/evidence gold and controlled labels.")
    review_coverage = metrics["answer_review_coverage"]
    check("all_outputs_independently_reviewed", review_coverage["distinct_answers"] > 0
          and review_coverage["independently_reviewed_answers"] == review_coverage["distinct_answers"],
          "Independently review all distinct initial, repaired, and final answers, blind to verdict and variant.")
    retrieval = metrics["required_evidence_coverage_at_8"]
    check("retrieval_coverage", retrieval["value"] is not None and retrieval["denominator"] == 140 and retrieval["numerator"] >= 126,
          "At least 126/140 cases need all required evidence in the packed top eight.")
    far = metrics["controlled"]["false_acceptance"]
    retention = metrics["controlled"]["supported_retention"]
    check("controlled_false_acceptance", far["denominator"] == 200 and far["numerator"] <= 10, "At most 10/200 unsupported original challenge claims accepted; no repair.")
    check("controlled_retention", retention["denominator"] == 200 and retention["numerator"] >= 180, "At least 180/200 supported originals accepted.")
    complete = d["correct_and_complete"]
    baseline = a["correct_and_complete"]
    check("correct_and_complete", complete["value"] is not None and baseline["value"] is not None
          and complete["denominator"] == 140 and complete["numerator"] >= 105
          and complete["numerator"] >= baseline["numerator"] - 7,
          "D needs at least 105/140 correct-complete answers, at most seven fewer than A.")
    for name, denominator, minimum in (("missing_evidence_handling", 40, 38), ("conflict_handling", 20, 19)):
        metric = d[name]
        check(name, metric["value"] is not None and metric["denominator"] == denominator and metric["numerator"] >= minimum,
              f"Require at least {minimum}/{denominator} appropriate final responses.")
    audit = d["final_response_audit"]
    check("release_audit", d["reviewed_substantive_releases"] >= 100 and audit["responses"] == 200
          and audit["pending"] == 0 and audit["materially_unsupported"] == 0,
          "Audit every final response, including conflict and abstention text; zero observed material unsupported responses.")
    completion = d["live_completion"]
    check("live_completion", completion["denominator"] == 200 and completion["numerator"] >= 198,
          "At least 198/200 complete with an answer or valid abstention; technical failures stay in the denominator.")
    # No guessed success from local fixture tests or cached paired timing. A future
    # qualification import must bind actual fault/SLO artifacts to this policy.
    check("mandatory_fault_suite", False, "A reviewed, policy-bound mandatory fault-suite artifact is not supplied by this evaluation runner.")
    check("live_concurrent_latency", False, "Paired replay does not establish live p95 at four concurrent requests; a separate live load result is required.")
    check("repeatability", False, "A predeclared twenty-question, three-repetition live artifact is required.")
    failed = [key for key, value in checks.items() if not value["passed"]]
    return {
        "qualified": False, "status": "unqualified", "primary_variant": "D",
        "human_reviewed": checks["question_and_controlled_gold_reviewed"]["passed"] and checks["all_outputs_independently_reviewed"]["passed"],
        "checks": checks, "unmet_requirements": failed,
        "qualification_note": "Engineering completion is separate from model quality. No production policy is auto-issued from fixtures or replay metrics.",
    }


def estimate_study(dataset: EvaluationDataset, config: Any, selected_splits: list[str]) -> dict[str, Any]:
    """Conservative planning counts. ProviderHub reserves actual calls durably."""
    questions = [q for q in dataset.questions if q.split in selected_splits]
    controlled = [q for q in dataset.controlled if q.split in selected_splits]
    repairs = 1 if _get(config, "verification", "max_content_repairs", default=1) else 0
    logical = {
        "query_embeddings": sum(q.retrieval_mode == "corpus" for q in questions),
        "initial_generation": len(questions), "initial_verification_batches": len(questions),
        "repair_generation": len(questions) * repairs, "repair_verification_batches": len(questions) * repairs,
        "controlled_verification_batches": len(controlled),
    }
    role_counts = {
        "embeddings": logical["query_embeddings"], "generator": logical["initial_generation"],
        "repair_generator": logical["repair_generation"],
        "verifier": logical["initial_verification_batches"] + logical["repair_verification_batches"] + len(controlled),
    }
    roles = {}
    total = 0.0
    known = True
    for role, count in role_counts.items():
        if not count:
            continue
        try:
            profile = config.role_profile(role)
        except (AttributeError, KeyError, ValueError):
            profile = config.role_profile("generator") if role == "repair_generator" and hasattr(config, "role_profile") else None
        input_limit = _get(profile, "max_input_tokens", default=_get(profile, "limits", "max_input_tokens"))
        output_limit = 0 if role == "embeddings" else _get(profile, "max_output_tokens", default=_get(profile, "limits", "max_output_tokens"))
        if _get(profile, "protocol") == "cloudflare_clef":
            output_limit = output_reservation(profile)
        input_rate = _get(profile, "pricing", "input_usd_per_million")
        output_rate = _get(profile, "pricing", "output_usd_per_million")
        attempts = _get(profile, "max_attempts", default=_get(profile, "limits", "max_attempts", default=2))
        maximum_cost = None
        if all(value is not None for value in (input_limit, output_limit, input_rate, output_rate)):
            maximum_cost = count * attempts * (input_limit * input_rate + output_limit * output_rate) / 1_000_000
            total += maximum_cost
        elif _runtime_mode(config) != "mock":
            known = False
        roles[role] = {"logical_calls_upper_bound": count, "attempts_per_call": attempts,
                       "input_token_cap_per_call": input_limit, "output_token_cap_per_call": output_limit,
                       "estimated_usd_upper_bound": maximum_cost}
    return {
        "logical_calls": logical, "roles": roles,
        "logical_call_upper_bound": sum(logical.values()),
        "estimated_usd_upper_bound": 0.0 if _runtime_mode(config) == "mock" else total if known else None,
        "pricing_known": known, "remote_attempt_cap": _get(config, "evaluation", "max_remote_attempts", default=3000),
        "interpretation": "Ceilings, not billed usage. Excludes ingestion and separate repeatability/live-load studies. Actual failures/retries remain in the ledger.",
    }


def _ledger_calls(store: Any, prefix: str) -> list[dict[str, Any]]:
    try:
        return store.get_calls(prefix=prefix)
    except TypeError:
        return [row for row in store.get_calls() if str(row.get("run_id", "")).startswith(prefix)]


def _budget_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    known_actual = 0.0
    conservative = 0.0
    unknown = 0
    for call in calls:
        actual = call.get("actual_cost", call.get("actual_cost_usd"))
        reserved = call.get("estimated_cost", call.get("estimated_cost_usd", 0))
        if actual is None:
            unknown += 1
            conservative += float(reserved or 0)
        else:
            known_actual += float(actual)
            conservative += float(actual)
    return {"attempts": len(calls), "known_actual_usd": known_actual,
            "unknown_usage_or_cost_attempts": unknown, "conservative_accounted_usd": conservative}


def _evaluation_costs(report: dict[str, Any], correct_complete: dict[str, Any]) -> dict[str, Any]:
    """Separate query from controlled cost; unknown usage never becomes free."""
    calls = report.get("ledger_calls", [])
    prefix = f"eval:{report['id']}:"
    question_calls = [row for row in calls if str(row.get("run_id", "")).startswith(prefix + "q:")]
    controlled_calls = [row for row in calls if str(row.get("run_id", "")).startswith(prefix + "c:")]
    question_cost = _budget_summary(question_calls)
    controlled_cost = _budget_summary(controlled_calls)
    observed = report.get("remote_attempts_observed", 0)
    complete_ledger = "ledger_calls" in report and len(calls) == observed
    mock = report.get("runtime_mode") == "mock"
    counted_cost = question_cost["conservative_accounted_usd"] if mock or complete_ledger else None
    declared = len(report.get("questions", []))
    attempted = sum(row.get("attempted") is True for row in report.get("questions", []))
    correct = correct_complete["numerator"] if correct_complete["value"] is not None else None
    usage_fields = {}
    for field in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"):
        values = [row.get("usage", {}).get(field) for row in question_calls if isinstance(row.get("usage"), dict)]
        values = [value for value in values if type(value) is int and value >= 0]
        usage_fields[field] = {"reported_sum": sum(values) if values else None, "calls_reporting_field": len(values)}
    return {
        "status": "fixture_only" if mock else "accounted" if complete_ledger else "incomplete_ledger",
        "question_calls": question_cost, "controlled_calls": controlled_cost,
        "query_usd_per_declared_question": counted_cost / declared if counted_cost is not None and declared else None,
        "query_usd_per_attempted_question": counted_cost / attempted if counted_cost is not None and attempted else None,
        "query_usd_per_D_correct_complete_answer": counted_cost / correct if counted_cost is not None and correct else None,
        "denominators": {"declared_questions": declared, "attempted_questions": attempted, "D_correct_complete_answers": correct},
        "question_token_fields": usage_fields,
        "unknown_question_usage_calls": sum(not isinstance(row.get("usage"), dict) for row in question_calls),
        "interpretation": "Conservatively accounted query cost includes failed calls, retries and repairs. Controlled-study cost is separate. Provider token field aliases are reported separately, never added together as independent usage. Unknown usage remains unknown.",
    }


def _unattempted_question(case: QuestionCase, reason: str) -> dict[str, Any]:
    return {
        "id": case.id, "family_id": case.family_id, "split": case.split,
        "expected_response": case.expected_response, "retrieval_mode": case.retrieval_mode,
        "required_evidence_covered": False if case.retrieval_mode == "corpus" and case.expected_response == "answerable" else None,
        "initial_draft": None, "repair_draft": None, "evidence": None,
        "initial_verification": None, "repair_verification": None, "repair_attempted": False,
        "variants": {v: _outcome("cancelled" if reason == "cancelled" else "failed", reason=reason) for v in VARIANTS},
        "timings_ms": {}, "attempts_used": 0, "calls": [], "attempted": False,
        "error_code": reason, "review_only": True,
    }


def _unattempted_controlled(case: ControlledCase, reason: str) -> dict[str, Any]:
    return {"id": case.id, "family_id": case.family_id, "split": case.split,
            "supported": case.supported, "original_id": case.original_id,
            "mutation_category": case.mutation_category, "accepted": False,
            "execution_status": reason, "repair_disabled": True, "attempted": False,
            "attempts_used": 0, "calls": [], "label_review": case.review.model_dump(mode="json")}


def _live_admission(config: Any, estimate: dict[str, Any]) -> None:
    if _runtime_mode(config) == "mock":
        return
    total = _get(config, "budgets", "total_max_estimated_cost_usd", default=0)
    phase = _get(config, "budgets", "phase_max_estimated_cost_usd", default={}).get("evaluation", 0)
    if total <= 0 or phase <= 0 or not estimate["pricing_known"]:
        raise DatasetError("Live evaluation requires known prices and positive total and evaluation budgets")


async def evaluate_dataset(
    path: str | Path, store: Any, hub: Any, config: Any, *, job_id: str | None = None,
    split: str | None = None, annotations_path: str | Path | None = None,
    worker_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Operator-path entry point. The HTTP job wrapper exposes bundled names only.

    Cases run sequentially to keep a study attempt cap strict even with provider
    retries; the application/provider pool still enforces its global concurrency.
    This orchestration timing does not claim a four-concurrent-user latency SLO.
    """
    dataset = load_dataset(path)
    if dataset.purpose == "synthetic_fixture" and _runtime_mode(config) != "mock":
        raise DatasetError("The bundled synthetic evaluation is mock-only")
    selection = split or ("demo" if dataset.purpose == "synthetic_fixture" else "test")
    if selection not in ("all", "development", "test", "demo"):
        raise DatasetError("Unknown evaluation split")
    selected = ["development", "test", "demo"] if selection == "all" else [selection]
    questions = [q for q in dataset.questions if q.split in selected]
    controlled = [c for c in dataset.controlled if c.split in selected]
    if not questions and not controlled:
        raise DatasetError("Selected evaluation split has no cases")
    estimate = estimate_study(dataset, config, selected)
    _live_admission(config, estimate)
    evaluation_id = job_id or str(uuid.uuid4())
    prefix = f"eval:{evaluation_id}:"
    # The store ledger retains attempt reservations across worker/process restarts.
    calls_before = _ledger_calls(store, prefix)
    already_used = len(calls_before) if _runtime_mode(config) == "live" else 0
    cap = _get(config, "evaluation", "max_remote_attempts", default=3000)
    per_case_cap = _get(config, "runtime", "max_remote_attempts_per_query", default=10)
    deadline = _get(config, "runtime", "query_deadline_seconds", default=60)
    reason = "interrupted_evaluation" if worker_job and worker_job.get("attempts", 1) > 1 else None
    report: dict[str, Any] = {
        "evaluation_version": EVALUATION_VERSION, "id": evaluation_id,
        "dataset_id": dataset.id, "dataset_hash": dataset.content_hash,
        "dataset_manifest": dataset.model_dump(mode="json"), "selected_splits": selected,
        "runtime_mode": _runtime_mode(config), "fixture_only": dataset.purpose == "synthetic_fixture" or _runtime_mode(config) == "mock",
        "primary_variant": "D", "identity": _identity(config),
        "score_threshold": _get(config, "verification", "score_threshold"),
        "dry_run_estimate": estimate, "questions": [], "controlled": [],
        "run_complete": False, "status": "running", "started_at_epoch": time.time(),
        "review_only": True, "warning": "A/B outputs and unreleased drafts are evaluation diagnostics, not verified user answers.",
    }
    consumed = already_used

    def stop_reason() -> str | None:
        if reason:
            return reason
        if worker_job:
            current = store.get_job(worker_job["id"])
            if current and (current.get("status") == "cancelled" or current.get("cancel_requested")):
                return "cancelled"
        if _runtime_mode(config) == "live" and consumed >= cap:
            return "budget_exhausted"
        return None

    for case in questions:
        reason = stop_reason()
        if reason:
            report["questions"].append(_unattempted_question(case, reason))
            continue
        allowance = per_case_cap if _runtime_mode(config) == "mock" else min(per_case_cap, cap - consumed)
        ctx = CallContext.for_seconds(prefix + "q:" + case.id, "evaluation", deadline, allowance)
        ctx.owner_job_id = worker_job["id"] if worker_job else None
        result = await run_question_case(case, dataset, store, hub, config, ctx)
        result["attempted"] = True
        report["questions"].append(result)
        consumed += ctx.attempts_used
        if result.get("error_code") in ("budget_exhausted", "cancelled"):
            reason = result["error_code"]
    for case in controlled:
        reason = stop_reason()
        if reason:
            report["controlled"].append(_unattempted_controlled(case, reason))
            continue
        allowance = per_case_cap if _runtime_mode(config) == "mock" else min(per_case_cap, cap - consumed)
        ctx = CallContext.for_seconds(prefix + "c:" + case.id, "evaluation", deadline, allowance)
        ctx.owner_job_id = worker_job["id"] if worker_job else None
        result = await run_controlled_case(case, dataset, hub, config, ctx)
        result["attempted"] = True
        report["controlled"].append(result)
        consumed += ctx.attempts_used
        if result.get("execution_status") in ("budget_exhausted", "cancelled"):
            reason = result["execution_status"]
    report["run_complete"] = reason is None
    report["status"] = "completed" if report["run_complete"] else "incomplete"
    report["incomplete_reason"] = reason
    report["finished_at_epoch"] = time.time()
    report["remote_attempts_observed"] = consumed if _runtime_mode(config) == "live" else 0
    ledger_calls = _ledger_calls(store, prefix)
    report["ledger_calls"] = _safe_calls(ledger_calls)
    report["ledger_summary"] = _budget_summary(ledger_calls)
    review_file = annotations_path or _get(config, "evaluation", "gold_path")
    annotations = _read_reviews(review_file, dataset.content_hash) if review_file else None
    report["answer_adjudications"] = annotations.model_dump(mode="json") if annotations else None
    report["metrics"] = compute_metrics(report, annotations)
    report["qualification"] = qualification_report(report, report["metrics"])
    report["unmeasured"] = ["four-concurrent-request live SLO", "predeclared held-out repeatability study", "mandatory fault-suite qualification artifact"]
    if report["fixture_only"]:
        report["unmeasured"].extend(["real model quality", "natural error prevalence"])
    if any(q.retrieval_mode == "inline_fixture" for q in questions):
        report["unmeasured"].append("retrieval quality for inline fixture questions")
    return report


def bundled_dataset_path(config: Any | None = None) -> Path:
    """Only the named demo is exposed to HTTP, never a user-submitted path."""
    candidate = PROJECT_DATA / "demo" / "dataset.json"
    if candidate.is_file():
        return candidate
    # Installed package deployments can point their private configuration to the
    # copied bundled assets. This path is never accepted from an HTTP payload.
    value = _get(config, "evaluation", "allowlisted_datasets", default={}).get("demo") if config else None
    if value and Path(value).is_file():
        loaded = load_dataset(value)
        if loaded.id == "synthetic-demo-v1" and loaded.purpose == "synthetic_fixture":
            return Path(value)
    raise DatasetError("Bundled demo dataset is not available in this installation")


async def run_evaluation(job: dict[str, Any], store: Any, hub: Any, config: Any) -> dict[str, Any]:
    payload = job.get("payload", {})
    if set(payload) != {"dataset"} or payload.get("dataset") != "demo":
        raise DatasetError("The evaluation API accepts only the bundled dataset named demo")
    if _runtime_mode(config) != "mock":
        raise DatasetError("The bundled demo evaluation cannot call live models")
    return await evaluate_dataset(bundled_dataset_path(config), store, hub, config,
                                  job_id=job["id"], worker_job=job)


def demo_documents(config: Any | None = None) -> list[dict[str, Any]]:
    """Inputs for ordinary upload/leased ingestion; never bypasses Store leases."""
    path = bundled_dataset_path(config)
    dataset = load_dataset(path)
    root = path.parent.resolve()
    documents = []
    for source in dataset.documents:
        file = (root / source.path).resolve()
        if not file.is_relative_to(root):
            raise DatasetError("Demo document path escapes the bundled dataset")
        documents.append({"name": file.name, "raw": file.read_bytes(), "media_type": source.media_type,
                          "fixture_document_id": source.id, "title": source.title})
    return documents


def propose_mutation(original: ControlledCase, old: str, new: str, category: str, mutation_id: str) -> dict[str, Any]:
    """Literal mutation proposal only: a person must establish the new label."""
    if original.supported is not True or original.original_id:
        raise DatasetError("Choose a supported original, not another mutation")
    if not old or original.claim.count(old) != 1 or old == new:
        raise DatasetError("Mutation text must replace exactly one distinct matching span")
    if category not in MUTATION_CATEGORIES:
        raise DatasetError("Unknown mutation category")
    if not mutation_id or mutation_id == original.id:
        raise DatasetError("Mutation needs a new ID")
    return {
        "id": mutation_id, "family_id": original.family_id, "split": original.split,
        "claim": original.claim.replace(old, new, 1), "evidence_ids": original.evidence_ids,
        "supported": None, "original_id": original.id, "mutation_category": category,
        "review": {"status": "unreviewed", "reviewer_ids": [], "adjudicator_id": None,
                   "reviewed_at": None, "notes": "Mechanical proposal only. Review naturalness and whether support actually changes."},
    }


def regrade_report(report: dict[str, Any], annotations: AnswerReviews) -> dict[str, Any]:
    """Apply hash-bound human review without rerunning any provider calls."""
    result = copy.deepcopy(report)
    result["metrics"] = compute_metrics(result, annotations)
    result["qualification"] = qualification_report(result, result["metrics"])
    result["adjudication_applied"] = True
    result["answer_adjudications"] = annotations.model_dump(mode="json")
    return result


def load_answer_reviews(path: str | Path, dataset_hash: str) -> AnswerReviews:
    """Read operator labels, enforcing their frozen dataset binding."""
    return _read_reviews(path, dataset_hash)


def apply_annotations(report_path: str | Path, annotations_path: str | Path) -> dict[str, Any]:
    """Offline CLI helper: apply actual adjudications to a saved paired report."""
    try:
        report = strict_json(Path(report_path).read_text(encoding="utf-8"))
        return regrade_report(report, _read_reviews(annotations_path, report["dataset_hash"]))
    except DatasetError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DatasetError("Report or answer-review file is invalid") from exc


def _timestamp(value: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _artifact(value: dict[str, Any] | str | Path | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        result = strict_json(Path(value).read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _artifact_binding(artifact: dict[str, Any], report: dict[str, Any], kind: str) -> list[str]:
    errors = []
    expected = {
        "schema_version": 1, "kind": kind, "evaluation_id": report["id"],
        "dataset_hash": report["dataset_hash"], "semantic_fingerprint": report["identity"]["policy_hash"],
        "implementation_fingerprint": report["identity"].get("implementation_fingerprint"),
    }
    for key, value in expected.items():
        if value is None or artifact.get(key) != value:
            errors.append(f"{key} mismatch")
    if artifact.get("complete") is not True:
        errors.append("artifact incomplete")
    if not isinstance(artifact.get("artifact_id"), str) or not artifact["artifact_id"].strip():
        errors.append("missing artifact ID")
    if _timestamp(artifact.get("generated_at")) is None:
        errors.append("missing dated artifact provenance")
    if "content_hash" in artifact and artifact["content_hash"] != stable_hash({key: value for key, value in artifact.items() if key != "content_hash"}):
        errors.append("artifact content hash mismatch")
    return errors


def _fault_artifact_checks(artifact: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    errors = _artifact_binding(artifact, report, "fault_suite")
    for name in ("native_postgres_verified", "restore_verified"):
        if artifact.get(name) is not True:
            errors.append(f"{name} not established")
    for name in ("migration_revision", "postgres_version", "pgvector_version"):
        if not artifact.get(name):
            errors.append(f"missing {name}")
    summary = artifact.get("summary", {})
    if (type(summary.get("passed")) is not int or summary["passed"] <= 0
            or any(type(summary.get(name)) is not int or summary[name] != 0 for name in ("failed", "skipped", "errors"))):
        errors.append("fault suite must pass with no failures, errors or skips")
    checks = artifact.get("checks", {})
    for name in FAULT_AREAS:
        item = checks.get(name, {}) if isinstance(checks, dict) else {}
        test_ids = item.get("test_ids", [])
        if (item.get("status") != "passed" or not isinstance(test_ids, list) or not test_ids
                or any(not isinstance(test, str) or "::" not in test for test in test_ids)):
            errors.append(f"mandatory fault area incomplete: {name}")
    if isinstance(checks, dict) and any(not isinstance(item, dict) or item.get("status") != "passed" for item in checks.values()):
        errors.append("a supplied fault check did not pass")
    return {"passed": not errors, "detail": "; ".join(errors) if errors else "All named fault areas, native PostgreSQL and restore checks passed.", "errors": errors}


def _finite_number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def _load_artifact_checks(artifact: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    errors = _artifact_binding(artifact, report, "live_load")
    if artifact.get("runtime_mode") != "live" or artifact.get("fixture_only") is not False:
        errors.append("load evidence must come from live endpoints")
    if artifact.get("concurrency") != 4:
        errors.append("load concurrency must be four requests")
    samples = artifact.get("samples", [])
    if not isinstance(samples, list):
        samples = []
    ids = [row.get("run_id") for row in samples if isinstance(row, dict)]
    if len(ids) != len(samples) or len(ids) != len(set(ids)) or any(not item for item in ids):
        errors.append("load samples need unique durable run IDs")
    selected_ids = {row["id"] for row in report["questions"]}
    strata: dict[tuple[str, bool], list[float]] = defaultdict(list)
    allowed_statuses = {"released", "answered", "abstained"}
    if artifact.get("verification_mode") in ("shadow", "evaluation"):
        allowed_statuses.add("shadow")
    for row in samples:
        if not isinstance(row, dict):
            errors.append("invalid load row")
            continue
        if (row.get("question_id") not in selected_ids or row.get("status") not in allowed_statuses
                or type(row.get("attempts")) is not int or not 0 <= row["attempts"] <= 10):
            errors.append("load case is unbound, incomplete or exceeds its attempt cap")
        total, queue = row.get("total_ms"), row.get("queue_ms")
        if (not _finite_number(total) or not _finite_number(queue)
                or not 0 <= queue <= total <= 60_000):
            errors.append("load timings must include queueing and respect the 60-second deadline")
            continue
        cache, repaired = row.get("cache_state"), row.get("repair_attempted")
        if cache not in ("cold", "warm") or type(repaired) is not bool:
            errors.append("cache and repair strata must be recorded explicitly")
            continue
        strata[(cache, repaired)].append(float(total))
    measurements = {}
    for cache in ("cold", "warm"):
        for repaired in (False, True):
            values = strata[(cache, repaired)]
            p95 = _quantile(values, 0.95)
            limit = 40_000 if repaired else 20_000
            measurements[f"{cache}_{'repair' if repaired else 'no_repair'}"] = {
                "cases": len(values), "p50_ms": _quantile(values, 0.5), "p95_ms": p95, "target_ms": limit,
            }
            if p95 is None or p95 > limit:
                errors.append(f"{cache} {'repair' if repaired else 'no-repair'} p95 target not established")
    if len(samples) < 4:
        errors.append("too few samples for four concurrent requests")
    return {"passed": not errors, "detail": "; ".join(dict.fromkeys(errors)) if errors else "Live queue-inclusive p95 meets both targets in cold and warm strata.",
            "measurements": measurements, "errors": list(dict.fromkeys(errors)),
            "sample_size_note": "No precision claim is inferred from a small latency sample; report every stratum count."}


def _repeatability_artifact_checks(artifact: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    errors = _artifact_binding(artifact, report, "repeatability")
    if artifact.get("runtime_mode") != "live" or artifact.get("fixture_only") is not False:
        errors.append("repeatability evidence must come from live endpoints")
    if artifact.get("measurement") != "verifier_repeatability":
        errors.append("repeatability must pin the original evidence and draft and vary only verifier execution")
    selected_ids = {row["id"] for row in report["questions"]}
    sample = artifact.get("question_ids", [])
    if (not isinstance(sample, list) or len(sample) != 20 or len(set(sample)) != 20
            or not set(sample).issubset(selected_ids) or artifact.get("repetitions") != 3):
        errors.append("repeatability requires twenty unique held-out IDs and three repetitions")
        sample = []
    declared = _timestamp(artifact.get("predeclared_at"))
    started = _timestamp(artifact.get("started_at"))
    if declared is None or started is None or declared > started:
        errors.append("sample must be recorded before repeatability execution starts")
    runs = artifact.get("runs", [])
    if not isinstance(runs, list):
        runs = []
    pair_ids = [(row.get("question_id"), row.get("repetition")) for row in runs if isinstance(row, dict)]
    expected = {(qid, repeat) for qid in sample for repeat in (1, 2, 3)}
    if len(runs) != 60 or len(pair_ids) != 60 or set(pair_ids) != expected or len(set(pair_ids)) != 60:
        errors.append("repeatability output dropped, duplicated or changed a planned case")
    run_ids = [row.get("run_id") for row in runs if isinstance(row, dict)]
    if len(set(run_ids)) != len(run_ids) or any(not item for item in run_ids):
        errors.append("repeatability attempts need distinct run IDs")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    frozen_inputs = {row["id"]: row for row in report["questions"]}
    failed = 0
    for row in runs:
        if not isinstance(row, dict):
            continue
        if row.get("status") not in ("ok", "released", "answered", "abstained"):
            failed += 1
        if not isinstance(row.get("decision"), str) or not row.get("answer_hash"):
            errors.append("repeatability rows must record the decision and exact final output hash, including failures")
        original = frozen_inputs.get(row.get("question_id"), {})
        raw_draft, raw_evidence = original.get("initial_draft"), original.get("evidence")
        if (not raw_draft or not raw_evidence or row.get("answer_hash") != stable_hash(raw_draft)
                or row.get("evidence_hash") != stable_hash(raw_evidence)):
            errors.append("repeatability input does not match this question's exact initial draft and evidence")
        grouped[row.get("question_id", "")].append(row)
    differing_decisions = sum(len({r.get("decision") for r in rows}) > 1 for rows in grouped.values())
    differing_answers = sum(len({r.get("answer_hash") for r in rows}) > 1 for rows in grouped.values())
    if failed:
        errors.append("A complete repeatability protocol needs an actual verifier decision for every planned attempt; technical failures remain recorded.")
    return {"passed": not errors, "detail": "; ".join(dict.fromkeys(errors)) if errors else "Predeclared 20×3 protocol completed; disagreement and failures are reported without a post-hoc threshold.",
            "errors": list(dict.fromkeys(errors)), "failed_attempts": failed, "attempted_cases": len(runs),
            "decision_disagreement_questions": differing_decisions, "answer_disagreement_questions": differing_answers,
            "question_denominator": len(sample)}


def qualify_policy(
    report: dict[str, Any], config: Any, *, fault_artifact: dict[str, Any] | str | Path | None,
    load_artifact: dict[str, Any] | str | Path | None,
    repeatability_artifact: dict[str, Any] | str | Path | None, policy_id: str,
) -> dict[str, Any]:
    """Produce a policy manifest only from complete, independently reviewed evidence.

    Artifact files are trusted operator records, not authenticated attestations.
    This importer verifies bindings and recomputes counts; it does not fabricate
    human labels, run endpoints, or infer missing native/latency evidence.
    """
    from evidence_lab.policy import semantic_policy_fingerprint
    dataset = EvaluationDataset.model_validate(report["dataset_manifest"])
    raw_annotations = report.get("answer_adjudications")
    annotations = AnswerReviews.model_validate(raw_annotations) if raw_annotations else None
    metrics = compute_metrics(report, annotations)
    decision = qualification_report(report, metrics)
    checks = decision["checks"]
    policy_hash = semantic_policy_fingerprint(config)
    checks["current_frozen_policy"] = {
        "passed": _runtime_mode(config) == "live" and report["identity"].get("policy_hash") == policy_hash
        and report["identity"].get("implementation_fingerprint") == implementation_fingerprint(),
        "detail": "The current live configuration, prompts, release policy and implementation must match the evaluated candidate.",
    }
    checks["policy_id"] = {"passed": isinstance(policy_id, str) and bool(policy_id.strip()), "detail": "A named policy ID is required."}
    protocol = config.role_profile("verifier").protocol
    threshold = config.verification.score_threshold
    selection = dataset.development_selection
    selected_at = _timestamp(selection.selected_at) if selection else None
    frozen_at = _timestamp(dataset.frozen_at)
    native = protocol == "cloudflare_clef"
    checks["protocol_acceptance_policy"] = {
        "passed": ((native and threshold is not None and threshold == dataset.preselected_score_threshold
                    and selected_at is not None and frozen_at is not None and selected_at <= frozen_at)
                   or (protocol == "chat_completions" and threshold is None and dataset.preselected_score_threshold is None)),
        "detail": "Clef needs an explicit native-score threshold recorded on development data before freezing; chat uses labels with no fabricated probability threshold.",
    }
    artifacts = {"fault_suite": _artifact(fault_artifact), "live_load": _artifact(load_artifact), "repeatability": _artifact(repeatability_artifact)}
    checks["mandatory_fault_suite"] = _fault_artifact_checks(artifacts["fault_suite"], report)
    checks["live_concurrent_latency"] = _load_artifact_checks(artifacts["live_load"], report)
    checks["repeatability"] = _repeatability_artifact_checks(artifacts["repeatability"], report)
    failed = [name for name, result in checks.items() if not result["passed"]]
    return {
        "schema_version": 1, "policy_id": policy_id, "semantic_fingerprint": policy_hash,
        "implementation_fingerprint": report["identity"].get("implementation_fingerprint"),
        "qualified": not failed, "status": "qualified" if not failed else "unqualified",
        "runtime_mode": report["runtime_mode"], "human_reviewed": decision["human_reviewed"],
        "evaluation_id": report["id"], "dataset_hash": report["dataset_hash"],
        "primary_variant": "D", "complete": report.get("run_complete") is True,
        "checks": checks, "unmet_requirements": failed,
        "artifact_hashes": {name: stable_hash(value) for name, value in artifacts.items() if value},
        "adjudications_hash": stable_hash(raw_annotations) if raw_annotations else None,
        "note": "Qualification applies only to the frozen measured PoC policy. Human/artifact authorship is an operator responsibility; source truth and production readiness are not established.",
    }


def dry_run_estimate(path: str | Path, config: Any, *, split: str | None = None) -> dict[str, Any]:
    """Validate and price a study without creating a job or making model calls."""
    dataset = load_dataset(path)
    selection = split or ("demo" if dataset.purpose == "synthetic_fixture" else "test")
    if selection not in ("all", "development", "test", "demo"):
        raise DatasetError("Unknown evaluation split")
    selected = ["development", "test", "demo"] if selection == "all" else [selection]
    if not any(case.split in selected for case in [*dataset.questions, *dataset.controlled]):
        raise DatasetError("Selected evaluation split has no cases")
    return {
        "dataset_id": dataset.id, "dataset_hash": dataset.content_hash,
        "purpose": dataset.purpose, "selected_splits": selected,
        "runtime_mode": _runtime_mode(config), "quality_qualified": False,
        **estimate_study(dataset, config, selected),
    }


def _csv_cell(value: Any) -> Any:
    # CSV is an interchange artifact; never turn an operator-controlled ID into
    # a spreadsheet formula when somebody opens the exported table.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, str]:
    """Export complete diagnostics plus human-readable tables; no inference.

    Existing files with these fixed names in the operator-selected directory are
    replaced. Raw drafts remain only in the explicitly review-only JSON report.
    """
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    files = {
        "json": destination / "report.json", "questions_csv": destination / "questions.csv",
        "controlled_csv": destination / "controlled.csv", "markdown": destination / "summary.md",
    }
    files["json"].write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    question_fields = ["question_id", "family_id", "split", "expected_response", "variant", "status",
                       "released", "answer_hash", "required_evidence_covered", "repair_attempted", "error_code"]
    with files["questions_csv"].open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=question_fields)
        writer.writeheader()
        for case in report["questions"]:
            for name in VARIANTS:
                outcome = case["variants"][name]
                row = {key: case.get(key) for key in question_fields}
                row.update(question_id=case["id"], variant=name, status=outcome["status"],
                           released=outcome["released"], answer_hash=outcome["answer_hash"])
                writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    controlled_fields = ["id", "family_id", "split", "supported", "original_id", "mutation_category",
                         "accepted", "execution_status", "attempted", "error_code"]
    with files["controlled_csv"].open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=controlled_fields)
        writer.writeheader()
        for case in report["controlled"]:
            writer.writerow({key: _csv_cell(case.get(key)) for key in controlled_fields})

    def metric_text(metric: dict[str, Any]) -> str:
        value = metric.get("value")
        result = f"{metric['numerator']}/{metric['denominator']}"
        if value is None:
            return result + f"; {metric.get('status', 'not measured')}; pending {metric.get('pending', 0)}"
        interval = metric.get("ci95_wilson")
        suffix = f"; 95% Wilson [{interval[0]:.1%}, {interval[1]:.1%}]" if interval else ""
        return result + f" ({value:.1%})" + suffix

    metrics = report["metrics"]
    qualification = report["qualification"]
    lines = [
        "# Evaluation report", "", f"Study: `{report['id']}`. Dataset hash: `{report['dataset_hash']}`.", "",
        f"Execution: **{report['status']}**. Runtime: **{report['runtime_mode']}**. "
        f"Quality qualification: **{qualification['status']}**.", "",
        "This report contains experimental diagnostics. A/B answers and rejected drafts are not verified user answers.", "",
    ]
    if report.get("fixture_only"):
        lines.extend(["**Synthetic/mock results check software behavior only. They do not measure real model quality.**", ""])
    lines.extend([
        "## Paired answer metrics", "",
        "Each case shares its initial evidence and decoded draft across A/B/C/D. Only D can perform one repair.", "",
        "| Variant | Correct and complete | Missing-evidence handling | Conflict handling | Operational completion |",
        "| --- | --- | --- | --- | --- |",
    ])
    for variant in VARIANTS:
        values = metrics["variants"][variant]
        lines.append("| " + " | ".join([variant, *[metric_text(values[key]) for key in (
            "correct_and_complete", "missing_evidence_handling", "conflict_handling", "live_completion",
        )]]) + " |")
    lines.extend([
        "", "## Retrieval and controlled claims", "",
        "All-required-evidence coverage at eight: " + metric_text(metrics["required_evidence_coverage_at_8"]) + ".", "",
        "Controlled false acceptance: " + metric_text(metrics["controlled"]["false_acceptance"]) + ".", "",
        "Controlled supported retention: " + metric_text(metrics["controlled"]["supported_retention"]) + ".", "",
        "Controlled claims are verified as originally supplied; repair is disabled. Failed attempts remain in their declared denominators.", "",
        "Wilson intervals assume independent cases. The JSON also reports paired differences using source-family bootstrap; "
        "a small number of families and zero observed events limit what those intervals establish.", "",
        "## Unmet qualification requirements", "",
    ])
    for name in qualification["unmet_requirements"]:
        detail = qualification["checks"][name]["detail"]
        lines.append(f"- **{name}**: {detail}")
    lines.extend(["", "## Unmeasured", ""])
    lines.extend(f"- {item}" for item in report.get("unmeasured", []))
    lines.append("")
    files["markdown"].write_text("\n".join(lines), encoding="utf-8")
    return {key: str(value) for key, value in files.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline evaluation review tools; no provider calls.")
    sub = parser.add_subparsers(dest="command", required=True)
    annotate = sub.add_parser("annotation-template", help="Make a blind, unfilled answer-review packet")
    annotate.add_argument("report", type=Path)
    annotate.add_argument("--output", type=Path, required=True)
    score = sub.add_parser("score", help="Apply completed answer reviews to a saved report")
    score.add_argument("report", type=Path)
    score.add_argument("annotations", type=Path)
    score.add_argument("--output", type=Path, required=True)
    inspect = sub.add_parser("inspect", help="Validate an operator dataset manifest without inference")
    inspect.add_argument("dataset", type=Path)
    mutate = sub.add_parser("propose-mutation", help="Propose one unreviewed literal mutation")
    mutate.add_argument("dataset", type=Path)
    mutate.add_argument("original_id")
    mutate.add_argument("--old", required=True)
    mutate.add_argument("--new", required=True)
    mutate.add_argument("--category", choices=MUTATION_CATEGORIES, required=True)
    mutate.add_argument("--id", dest="mutation_id", required=True)
    mutate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            dataset = load_dataset(args.dataset)
            output = {"id": dataset.id, "hash": dataset.content_hash, "purpose": dataset.purpose,
                      "questions": len(dataset.questions), "controlled": len(dataset.controlled),
                      "independent_gold_review_claimed": all(q.gold.review.independently_reviewed for q in dataset.questions)
                      and all(c.review.independently_reviewed for c in dataset.controlled),
                      "quality_qualified": False}
            print(json.dumps(output, indent=2))
            return 0
        if args.command == "propose-mutation":
            dataset = load_dataset(args.dataset)
            original = next((c for c in dataset.controlled if c.id == args.original_id), None)
            if original is None:
                raise DatasetError("Controlled original ID was not found")
            output = propose_mutation(original, args.old, args.new, args.category, args.mutation_id)
        else:
            report = strict_json(args.report.read_text(encoding="utf-8"))
            output = annotation_template(report) if args.command == "annotation-template" else regrade_report(
                report, _read_reviews(args.annotations, report["dataset_hash"]),
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return 0
    except (DatasetError, OSError, ValueError, KeyError) as exc:
        parser.error(str(exc) if isinstance(exc, DatasetError) else "Input file or output contract is invalid")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
