"""Exact hybrid retrieval, deterministic fusion and one shared evidence pack."""
from __future__ import annotations

import json

from evidence_lab.domain import CallContext, EvidenceItem, EvidencePack, ProviderError, stable_hash
from evidence_lab.ingestion import pipeline_revision, text_hash, validate_vectors


def space_manifest(config) -> dict:
    """Semantic identity excludes credentials and operational timeout settings.

    Endpoint changes conservatively require another declared space or a reindex.
    Mock fixtures and live embeddings never share an embedding identity.
    """
    profile = config.role_profile("embeddings")
    identity = {
        "embedding_space": profile.embedding_space,
        "model": profile.model,
        "semantic_revision": profile.semantic_revision,
        "dimensions": profile.dimensions,
        "endpoint": profile.endpoint,
        "protocol": profile.protocol,
        "runtime": config.runtime.mode,
        "preprocessing": "nfc-lf-exact-chunk-v1",
        "pipeline_revision": pipeline_revision(config),
        "distance": "cosine",
    }
    return {"id": profile.embedding_space, "dimensions": profile.dimensions, "model": profile.model, "fingerprint": stable_hash(identity)}


def _candidate(raw: dict | EvidenceItem) -> EvidenceItem:
    if isinstance(raw, EvidenceItem):
        item = raw.model_copy(deep=True)
    else:
        fields = EvidenceItem.model_fields
        try:
            item = EvidenceItem.model_validate({key: value for key, value in raw.items() if key in fields})
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("storage_integrity", "Retrieved candidate does not satisfy the evidence contract") from exc
    if not item.text_hash:
        item.text_hash = text_hash(item.text)
    if not item.id or not item.document_id or not item.version_id or item.page < 1:
        raise ProviderError("storage_integrity", "Retrieved evidence has invalid source identity")
    if item.start < 0 or item.end <= item.start or item.end - item.start != len(item.text):
        raise ProviderError("storage_integrity", "Retrieved evidence has invalid source coordinates")
    if item.text_hash != text_hash(item.text):
        raise ProviderError("storage_integrity", "Retrieved evidence hash does not match its content")
    return item


def _same_content(first: EvidenceItem, second: EvidenceItem) -> bool:
    keys = ("id", "document_id", "version_id", "title", "text", "page", "start", "end", "text_hash")
    return all(getattr(first, key) == getattr(second, key) for key in keys)


def _redundant(first: EvidenceItem, second: EvidenceItem) -> bool:
    # Identical wording from different sources may carry different provenance or
    # applicability. Deduplication must not discard that distinction.
    if first.version_id != second.version_id:
        return False
    if first.text_hash == second.text_hash and first.text == second.text:
        return True
    if first.page != second.page:
        return False
    intersection = max(0, min(first.end, second.end) - max(first.start, second.start))
    if intersection:
        start = max(first.start, second.start)
        end = min(first.end, second.end)
        if first.text[start - first.start:end - first.start] != second.text[start - second.start:end - second.start]:
            raise ProviderError("storage_integrity", "Overlapping chunks disagree on immutable source content")
    smaller = min(first.end - first.start, second.end - second.start)
    return smaller > 0 and intersection / smaller >= 0.8


def fuse_candidates(dense: list, lexical: list, k: int = 60) -> list[EvidenceItem]:
    """RRF each ID once per branch, break ties by ID, then remove overlap.

    Small intentional overlap between adjacent chunks is retained. A near-copy
    covering at least 80% of the smaller interval is omitted. Exact duplicate
    text from the same source version is also omitted. Different source versions
    retain their provenance even when the wording happens to be identical.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("RRF constant must be a positive integer")
    merged: dict[str, EvidenceItem] = {}
    ranks: dict[str, dict[str, int]] = {}
    for branch, candidates in (("dense", dense), ("lexical", lexical)):
        for position, raw in enumerate(candidates, 1):
            item = _candidate(raw)
            rank = raw.get("rank", position) if isinstance(raw, dict) else position
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                raise ProviderError("storage_integrity", "Retrieved branch rank must be a positive integer")
            if item.id in merged and not _same_content(merged[item.id], item):
                raise ProviderError("storage_integrity", "Retrieval branches disagree on immutable evidence content")
            merged.setdefault(item.id, item)
            previous = ranks.setdefault(item.id, {}).get(branch)
            ranks[item.id][branch] = min(previous, rank) if previous is not None else rank
    for chunk_id, item in merged.items():
        item.dense_rank = ranks[chunk_id].get("dense")
        item.lexical_rank = ranks[chunk_id].get("lexical")
        item.fused_score = sum(1.0 / (k + rank) for rank in ranks[chunk_id].values())
    ordered = sorted(merged.values(), key=lambda item: (-item.fused_score, item.id))
    kept: list[EvidenceItem] = []
    for item in ordered:
        if not any(_redundant(item, earlier) for earlier in kept):
            kept.append(item)
    return kept


def evidence_size(pack: EvidencePack) -> int:
    return len(json.dumps(pack.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def pack_evidence(candidates: list[EvidenceItem], *, corpus_id: str, space_id: str, corpus_revision: int, budget_bytes: int, max_items: int, all_ids: list[str] | None = None) -> EvidencePack:
    """Pack whole immutable chunks; no text is truncated to force a fit."""
    if not isinstance(budget_bytes, int) or budget_bytes <= 0:
        raise ProviderError("over_budget", "Question and reserved outputs leave no shared evidence budget")
    if max_items < 1:
        raise ValueError("Evidence chunk limit must be positive")
    identifiers = list(dict.fromkeys(all_ids if all_ids is not None else [item.id for item in candidates]))
    pack = EvidencePack(corpus_id=corpus_id, space_id=space_id, corpus_revision=corpus_revision, items=[], omitted_ids=identifiers)
    if evidence_size(pack) > budget_bytes:
        raise ProviderError("over_budget", "Shared context budget cannot fit evidence metadata")
    for item in candidates:
        if len(pack.items) >= max_items:
            break
        candidate = pack.model_copy(deep=True)
        candidate.items.append(item.model_copy(deep=True))
        candidate.omitted_ids = [identifier for identifier in identifiers if identifier not in {selected.id for selected in candidate.items}]
        if evidence_size(candidate) <= budget_bytes:
            pack = candidate
    if candidates and not pack.items:
        raise ProviderError("over_budget", "No complete retrieved chunk fits the shared context budget")
    return pack


async def _retrieve_snapshot(question: str, corpus_id: str, store, hub, config, ctx: CallContext) -> tuple[dict, str, int]:
    """Pin, embed outside the DB, and recheck the same embedding space.

    A changed space aborts explicitly. The caller may schedule a new attempt;
    this function never silently compares vectors from different spaces.
    """
    ctx.remaining()
    pinned = store.get_corpus(corpus_id)
    manifest = space_manifest(config)
    if pinned["space_id"] != manifest["id"]:
        raise ProviderError("space_changed", "Query embedding configuration does not match the active corpus space")
    # ensure_corpus also validates the fingerprint for an existing space ID.
    store.ensure_corpus(corpus_id, manifest)
    budget = hub.shared_evidence_budget(question)
    vectors = validate_vectors(await hub.embed([question], ctx), 1, manifest["dimensions"])
    ctx.remaining()
    snapshot = store.retrieve(
        corpus_id,
        pinned["space_id"],
        question,
        vectors[0],
        dense_limit=config.retrieval.dense_candidates,
        lexical_limit=config.retrieval.lexical_candidates,
    )
    # The DB implementation does this inside its repeatable-read transaction;
    # honor an explicitly returned space identity as an additional invariant.
    if snapshot.get("space_id", pinned["space_id"]) != pinned["space_id"]:
        raise ProviderError("space_changed", "Corpus embedding space changed during retrieval")
    return snapshot, pinned["space_id"], budget


def _pack_snapshot(snapshot, space_id, budget, corpus_id, config, branch="fused") -> EvidencePack:
    dense = snapshot["dense"] if branch != "lexical" else []
    lexical = snapshot["lexical"] if branch != "dense" else []
    candidates = fuse_candidates(dense, lexical, config.retrieval.rrf_constant)
    all_ids = list(dict.fromkeys(item.id if isinstance(item, EvidenceItem) else item["id"] for item in dense + lexical))
    return pack_evidence(
        candidates,
        corpus_id=corpus_id,
        space_id=space_id,
        corpus_revision=snapshot["corpus_revision"],
        budget_bytes=budget,
        max_items=config.retrieval.evidence_chunks,
        all_ids=all_ids,
    )


async def retrieve_evidence(question: str, corpus_id: str, store, hub, config, ctx: CallContext) -> EvidencePack:
    """Retrieve and freeze the fused evidence pack used throughout one answer."""
    snapshot, space_id, budget = await _retrieve_snapshot(question, corpus_id, store, hub, config, ctx)
    return _pack_snapshot(snapshot, space_id, budget, corpus_id, config)


async def retrieve_evidence_variants(question: str, corpus_id: str, store, hub, config, ctx: CallContext) -> dict[str, EvidencePack]:
    """Evaluation-only dense/lexical/fused packs from one actual DB snapshot.

    Every branch uses the same context ceiling and whole-chunk rules. This does
    not pretend that dense or lexical baselines can be reconstructed from the
    already truncated fused top eight, and incurs only one query embedding.
    """
    snapshot, space_id, budget = await _retrieve_snapshot(question, corpus_id, store, hub, config, ctx)
    return {branch: _pack_snapshot(snapshot, space_id, budget, corpus_id, config, branch) for branch in ("fused", "dense", "lexical")}
