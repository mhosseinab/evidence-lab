from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from pydantic import SecretStr

from evidence_lab.config import AppConfig, Capabilities, Profile, Roles
from evidence_lab.domain import CallContext, ProviderError
from evidence_lab.ingestion import text_hash
from evidence_lab.retrieval import evidence_size, fuse_candidates, pack_evidence, retrieve_evidence, retrieve_evidence_variants, space_manifest


def config():
    return AppConfig(
        profiles={
            "e": Profile(protocol="embeddings", endpoint="https://embed.example/v1/embeddings", model="fixture-embed", embedding_space="fixture-v1", dimensions=3, max_input_tokens=10000),
            "g": Profile(protocol="chat_completions", max_input_tokens=200000, max_output_tokens=1200, capabilities=Capabilities()),
            "v": Profile(protocol="chat_completions", max_input_tokens=200000, max_output_tokens=1600, capabilities=Capabilities()),
        },
        roles=Roles(embeddings="e", generator="g", verifier="v"),
    )


def hit(identifier, text=None, *, rank=None, version=None, start=0, page=1):
    text = text if text is not None else f"Evidence for {identifier}."
    result = {"id": identifier, "document_id": f"d-{version or identifier}", "version_id": version or f"v-{identifier}", "title": "Fixture document", "text": text, "page": page, "start": start, "end": start + len(text), "text_hash": text_hash(text)}
    if rank is not None:
        result["rank"] = rank
    return result


def test_rrf_counts_once_per_branch_preserves_ranks_and_breaks_ties_by_id():
    dense = [hit("b", rank=1), hit("a", rank=2), hit("a", rank=4)]
    lexical = [hit("a", rank=1), hit("b", rank=2)]
    fused = fuse_candidates(dense, lexical)
    assert [item.id for item in fused] == ["a", "b"]
    assert fused[0].dense_rank == 2 and fused[0].lexical_rank == 1
    assert fused[0].fused_score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].fused_score == fused[0].fused_score


def test_empty_branches_are_valid_and_no_fake_candidates_are_added():
    assert fuse_candidates([], []) == []
    assert [item.id for item in fuse_candidates([], [hit("a")])] == ["a"]
    assert [item.id for item in fuse_candidates([hit("a")], [])] == ["a"]


def test_overlap_dedupe_keeps_adjacent_chunks_with_substantial_new_content():
    original = hit("a", "A" * 100, version="same", start=0)
    overlap = hit("b", "A" * 90, version="same", start=10)
    adjacent = hit("c", "A" * 20 + "B" * 80, version="same", start=80)
    fused = fuse_candidates([original, overlap, adjacent], [])
    assert [item.id for item in fused] == ["a", "c"]


def test_identical_text_from_distinct_sources_keeps_both_provenances():
    fused = fuse_candidates([hit("a", "Effective from January."), hit("b", "Effective from January.")], [])
    assert [item.id for item in fused] == ["a", "b"]


def test_overlapping_text_must_agree_before_deduplication():
    with pytest.raises(ProviderError, match="Overlapping"):
        fuse_candidates([hit("a", "A" * 100, version="same"), hit("b", "B" * 90, version="same", start=10)], [])


def test_inconsistent_snapshot_or_bad_coordinates_fail_closed():
    with pytest.raises(ProviderError, match="disagree"):
        fuse_candidates([hit("a", "First assertion.")], [hit("a", "Different assertion.")])
    invalid = hit("a")
    invalid["end"] += 1
    with pytest.raises(ProviderError, match="coordinates"):
        fuse_candidates([invalid], [])
    invalid = hit("a")
    invalid["text_hash"] = "wrong"
    with pytest.raises(ProviderError, match="hash"):
        fuse_candidates([invalid], [])


@pytest.mark.parametrize("rank", [0, -1, True, 1.2, "1"])
def test_invalid_branch_ranks_are_rejected(rank):
    with pytest.raises(ProviderError):
        fuse_candidates([hit("a", rank=rank)], [])


def test_shared_budget_counts_full_json_and_retains_whole_chunks_only():
    candidates = fuse_candidates([hit("a", "Short statement."), hit("b", "𐍈" * 300)], [])
    all_pack = pack_evidence(candidates, corpus_id="c", space_id="s", corpus_revision=1, budget_bytes=10000, max_items=8)
    one_pack = pack_evidence(candidates, corpus_id="c", space_id="s", corpus_revision=1, budget_bytes=10000, max_items=1)
    result = pack_evidence(candidates, corpus_id="c", space_id="s", corpus_revision=1, budget_bytes=evidence_size(one_pack), max_items=8)
    assert evidence_size(all_pack) > evidence_size(one_pack)
    assert [item.id for item in result.items] == ["a"]
    assert result.items[0].text == candidates[0].text
    assert result.omitted_ids == ["b"]
    assert evidence_size(result) <= evidence_size(one_pack)


def test_no_fitting_chunk_is_over_budget_not_a_false_empty_retrieval():
    candidates = fuse_candidates([hit("a", "x" * 1000)], [])
    with pytest.raises(ProviderError) as caught:
        pack_evidence(candidates, corpus_id="c", space_id="s", corpus_revision=1, budget_bytes=300, max_items=8)
    assert caught.value.status == "over_budget"
    empty = pack_evidence([], corpus_id="c", space_id="s", corpus_revision=1, budget_bytes=300, max_items=8)
    assert empty.items == []


def test_evidence_pack_is_an_immutable_content_snapshot_by_value():
    candidate = fuse_candidates([hit("a")], [])[0]
    pack = pack_evidence([candidate], corpus_id="c", space_id="s", corpus_revision=1, budget_bytes=10000, max_items=8)
    before = pack.content_hash
    candidate.text = "Replacement source content"
    assert pack.content_hash == before
    assert pack.items[0].text == "Evidence for a."


def test_embedding_manifest_ignores_key_rotation_but_binds_semantics_and_mode():
    cfg = config()
    first = space_manifest(cfg)
    cfg.profiles["e"].api_key = SecretStr("rotated-key")
    cfg.profiles["e"].timeout_seconds = 45
    assert space_manifest(cfg) == first
    cfg.profiles["e"].model = "different-model"
    assert space_manifest(cfg)["fingerprint"] != first["fingerprint"]
    cfg = config()
    cfg.profiles["e"].endpoint = "https://other.example/v1/embeddings"
    assert space_manifest(cfg)["fingerprint"] != first["fingerprint"]
    cfg = config()
    cfg.runtime.mode = "live"
    assert space_manifest(cfg)["fingerprint"] != first["fingerprint"]


class RetrievalStore:
    def __init__(self, cfg):
        self.manifest = space_manifest(cfg)
        self.corpus = {"id": "default", "space_id": self.manifest["id"], "revision": 1}
        self.candidates = [hit("a")]
        self.retrieval_calls = []

    def get_corpus(self, corpus_id):
        return deepcopy(self.corpus)

    def ensure_corpus(self, corpus_id, manifest):
        if manifest != self.manifest:
            raise ProviderError("space_changed", "Manifest mismatch")
        return deepcopy(self.corpus)

    def retrieve(self, corpus_id, space_id, query, vector, **kwargs):
        self.retrieval_calls.append({"space_id": space_id, "query": query, "vector": vector})
        if space_id != self.corpus["space_id"]:
            raise ProviderError("space_changed", "Active embedding space changed")
        return {"corpus_revision": self.corpus["revision"], "space_id": space_id, "dense": deepcopy(self.candidates), "lexical": []}


class RetrievalHub:
    def __init__(self, during_embed=None):
        self.during_embed = during_embed
        self.embed_calls = 0

    def shared_evidence_budget(self, question):
        return 20000

    async def embed(self, texts, ctx):
        self.embed_calls += 1
        if self.during_embed:
            self.during_embed()
        return [[1.0, 0.0, 0.5]]


def test_space_change_during_query_embedding_aborts_instead_of_mixing_vectors():
    cfg = config()
    store = RetrievalStore(cfg)
    hub = RetrievalHub(lambda: store.corpus.update(space_id="other-space"))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(retrieve_evidence("Question", "default", store, hub, cfg, CallContext.for_seconds("r1", "queries")))
    assert caught.value.status == "space_changed"
    assert hub.embed_calls == 1


def test_snapshot_revision_may_advance_without_embedding_space_change():
    cfg = config()
    store = RetrievalStore(cfg)
    hub = RetrievalHub(lambda: store.corpus.update(revision=2))
    pack = asyncio.run(retrieve_evidence("Question", "default", store, hub, cfg, CallContext.for_seconds("r1", "queries")))
    assert pack.corpus_revision == 2
    assert pack.items[0].id == "a"
    store.candidates[0]["text"] = "Changed after retrieval"
    assert pack.items[0].text == "Evidence for a."


def test_mismatched_embedding_config_fails_before_remote_call():
    cfg = config()
    store = RetrievalStore(cfg)
    cfg.profiles["e"].model = "unindexed-model"
    hub = RetrievalHub()
    with pytest.raises(ProviderError):
        asyncio.run(retrieve_evidence("Question", "default", store, hub, cfg, CallContext.for_seconds("r1", "queries")))
    assert hub.embed_calls == 0


def test_retrieval_ablation_packs_share_one_embedding_and_snapshot():
    cfg = config()
    store = RetrievalStore(cfg)
    hub = RetrievalHub()
    variants = asyncio.run(retrieve_evidence_variants("Question", "default", store, hub, cfg, CallContext.for_seconds("r1", "evaluation")))
    assert set(variants) == {"fused", "dense", "lexical"}
    assert [item.id for item in variants["fused"].items] == ["a"]
    assert [item.id for item in variants["dense"].items] == ["a"]
    assert variants["lexical"].items == []
    assert hub.embed_calls == 1 and len(store.retrieval_calls) == 1
    assert {pack.corpus_revision for pack in variants.values()} == {1}
