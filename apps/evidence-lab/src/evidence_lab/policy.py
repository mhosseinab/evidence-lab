"""Deterministic gate rules and explicit policy qualification."""
from __future__ import annotations

import re
import hashlib
from pathlib import Path

from evidence_lab.domain import GLOBAL_IDS, PROMPT_VERSION, SCHEMA_VERSION, Draft, EvidencePack, VerificationResult, stable_hash, strict_json


def structural_check(draft: Draft, evidence: EvidencePack) -> dict:
    allowed = {item.id: item for item in evidence.items}
    failures: list[str] = []
    if len(allowed) != len(evidence.items):
        failures.append("evidence:duplicate_id")
    for item in evidence.items:
        if item.text_hash and hashlib.sha256(item.text.encode("utf-8")).hexdigest() != item.text_hash:
            failures.append("evidence:hash_mismatch")
        if item.start < 0 or item.end < item.start or item.page < 1:
            failures.append("evidence:invalid_coordinates")
    for block in draft.blocks:
        missing = [c for c in block.citation_ids if c not in allowed]
        if missing:
            failures.append(f"{block.block_id}:unknown_citation")
        cited = [allowed[c].text for c in block.citation_ids if c in allowed]
        # Explicit quotations must be verbatim in a cited chunk. Semantic paraphrases
        # are handled by the verifier, not by fuzzy string matching here.
        for quote in re.findall(r'"([^"\n]+)"|“([^”\n]+)”', block.text):
            text = quote[0] or quote[1]
            if text and not any(text in source for source in cited):
                failures.append(f"{block.block_id}:unmatched_quote")
        if not block.text.strip():
            failures.append(f"{block.block_id}:empty_text")
    return {"accepted": not failures, "failures": failures}


def evaluate_checks(draft: Draft, evidence: EvidencePack, result: VerificationResult,
                    threshold: float | None = None, *, expected_round_id: str | None = None) -> dict:
    structural = structural_check(draft, evidence)
    failures = list(structural["failures"])
    failed_ids: list[str] = []
    technical: list[str] = []
    if result.execution_status != "ok":
        technical.append(result.execution_status)
    if result.answer_hash != draft.content_hash:
        technical.append("answer_hash_mismatch")
    if result.evidence_hash != evidence.content_hash:
        technical.append("evidence_hash_mismatch")
    if not result.round_id:
        technical.append("missing_round_id")
    elif expected_round_id is not None and result.round_id != expected_round_id:
        technical.append("round_id_mismatch")
    expected_blocks = {b.block_id for b in draft.blocks}
    expected = expected_blocks | set(GLOBAL_IDS)
    ids = [c.id for c in result.checks]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        technical.append("incomplete_coverage")
    for check in result.checks:
        kind = "block_support" if check.id in expected_blocks else "global"
        if check.kind != kind:
            technical.append(f"{check.id}:wrong_kind")
        passed = check.support_status == "supported" if kind == "block_support" else check.check_status == "pass"
        if not passed:
            failures.append(check.id)
            failed_ids.append(check.id)
        if threshold is not None:
            if check.support_score is None:
                technical.append(f"{check.id}:missing_native_score")
            elif check.support_score < threshold:
                failures.append(f"{check.id}:below_threshold")
                failed_ids.append(check.id)
    return {"accepted": not failures and not technical, "failures": failures,
            "failed_check_ids": list(dict.fromkeys(failed_ids)), "technical_failures": technical,
            "checks": [c.model_dump(mode="json") for c in result.checks]}


def semantic_policy_fingerprint(config) -> str:
    selected = {}
    semantic_fields = ("protocol", "endpoint", "model", "dimensions", "embedding_space", "semantic_revision",
                       "capabilities", "extra_body", "embedding_preprocessing", "max_input_tokens",
                       "max_output_tokens", "max_batch_input_tokens", "batch_size", "request_dimensions",
                       "max_questions", "token_counting", "context_headroom_fraction",
                       "timeout_seconds", "max_attempts", "concurrency")
    for role in ("embeddings", "generator", "verifier", "repair_generator"):
        profile = config.role_profile(role)
        raw = profile.model_dump(mode="json")
        selected[role] = {k: raw.get(k) for k in semantic_fields}
    verification = config.verification
    payload = {
        "profiles": selected,
        "retrieval": config.retrieval.model_dump(mode="json"),
        "ingestion": config.ingestion.model_dump(mode="json"),
        "query_runtime": {
            "deadline_seconds": config.runtime.query_deadline_seconds,
            "max_remote_attempts": config.runtime.max_remote_attempts_per_query,
            "remote_concurrency": config.runtime.remote_concurrency,
        },
        "threshold": verification.score_threshold,
        "max_answer_blocks": verification.max_answer_blocks,
        "max_answer_bytes": getattr(verification, "max_answer_bytes", 8000),
        "max_repair_bytes": getattr(verification, "max_repair_bytes", 16384),
        "max_content_repairs": verification.max_content_repairs,
        "evidence_policy": verification.evidence_policy,
        "global_checks": GLOBAL_IDS,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    return stable_hash(payload)


def policy_state(config) -> dict:
    """Mock qualification is never transferable to live mode."""
    if config.runtime.mode == "mock":
        return {"state": "fixture_only", "release_allowed": config.verification.mode == "gated",
                "qualified": False, "reason": "Deterministic fixtures test plumbing, not model quality."}
    if config.verification.mode != "gated":
        return {"state": config.verification.mode, "release_allowed": False,
                "qualified": False, "reason": "Candidate policy; drafts are diagnostic only."}
    path = getattr(config.verification, "policy_path", None)
    policy_id = config.verification.policy_id
    if not path or not policy_id:
        return {"state": "unqualified", "release_allowed": False, "qualified": False,
                "reason": "A gated live profile requires a policy artifact and policy ID."}
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Policy artifact too large")
        artifact = strict_json(raw.decode("utf-8"))
        if not isinstance(artifact, dict):
            raise ValueError("Policy artifact must be an object")
        from evidence_lab.evaluation import implementation_fingerprint
        valid = (
            artifact.get("policy_id") == policy_id
            and artifact.get("semantic_fingerprint") == semantic_policy_fingerprint(config)
            and artifact.get("qualified") is True
            and artifact.get("runtime_mode") == "live"
            and artifact.get("human_reviewed") is True
            and bool(artifact.get("evaluation_id"))
            and artifact.get("primary_variant") == "D"
            and artifact.get("complete") is True
            and artifact.get("implementation_fingerprint") == implementation_fingerprint()
        )
        if not valid:
            raise ValueError("Policy does not match the selected evaluated configuration")
    except (OSError, ValueError, TypeError, RecursionError):
        return {"state": "unqualified", "release_allowed": False, "qualified": False,
                "reason": "Policy artifact is missing, incomplete or does not match the configuration."}
    return {"state": "qualified", "release_allowed": True, "qualified": True,
            "reason": "Matched to the supplied held-out evaluation artifact."}
