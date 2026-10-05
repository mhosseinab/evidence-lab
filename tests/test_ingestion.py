"""Contract/fault tests; deterministic providers do not measure model quality."""
from __future__ import annotations

import asyncio
import io
import os
import uuid
from copy import deepcopy

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from rag_poc.config import AppConfig, Capabilities, Profile, Roles
from rag_poc.domain import ProviderError
from rag_poc.ingestion import admit_document, extract_document, ingest_job, pipeline_revision, text_hash, validate_vectors
from rag_poc.retrieval import space_manifest


def config():
    return AppConfig(
        profiles={
            "e": Profile(protocol="embeddings", model="fixture-embed", embedding_space="fixture-v1", dimensions=3, max_input_tokens=100000, max_batch_input_tokens=100000, batch_size=2),
            "g": Profile(protocol="chat_completions", model="fixture-chat", max_input_tokens=200000, max_output_tokens=1200, capabilities=Capabilities()),
            "v": Profile(protocol="chat_completions", model="fixture-verifier", max_input_tokens=200000, max_output_tokens=1600, capabilities=Capabilities()),
        },
        roles=Roles(embeddings="e", generator="g", verifier="v"),
    )


def make_pdf(page_types):
    writer = PdfWriter()
    for kind in page_types:
        page = writer.add_blank_page(width=300, height=300)
        if kind == "blank":
            continue
        resources = DictionaryObject()
        commands = b""
        if "text" in kind:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
            commands += b"BT /F1 12 Tf 20 250 Td (Do not exceed 5 mg daily.) Tj ET\n"
        if "image" in kind:
            image = DecodedStreamObject()
            image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"), NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1), NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
            image.set_data(b"\x00\x00\x00")
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im0"): writer._add_object(image)})
            commands += b"q 10 0 0 10 10 10 cm /Im0 Do Q\n"
        if kind == "drawing":
            commands = b"10 10 20 20 re f\n"
        stream = DecodedStreamObject()
        stream.set_data(commands)
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = resources
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_chunk_ranges_preserve_negation_units_and_minimal_normalization():
    cfg = config()
    cfg.ingestion.chunk_target_chars = 70
    cfg.ingestion.chunk_max_chars = 100
    cfg.ingestion.chunk_overlap_chars = 15
    source = ("Do not exceed −5 mg. Cafe\u0301 remains open.\r\n\r\n" * 15)
    result = extract_document(source.encode(), "policy.md", "text/markdown", cfg)
    assert result["state"] == "extracted"
    normalized = result["pages"][0]["text"]
    assert "Do not exceed −5 mg." in normalized
    assert "Café" in normalized and "\r" not in normalized
    assert len(result["chunks"]) > 2
    covered = set()
    for chunk in result["chunks"]:
        assert chunk["text"] == normalized[chunk["start"]:chunk["end"]]
        assert len(chunk["text"]) <= cfg.ingestion.chunk_max_chars
        assert chunk["text_hash"] == text_hash(chunk["text"])
        covered.update(range(chunk["start"], chunk["end"]))
    assert all(index in covered for index, char in enumerate(normalized) if not char.isspace())
    renamed = extract_document(source.encode(), "renamed.txt", "text/plain", cfg)
    assert [c["id"] for c in result["chunks"]] == [c["id"] for c in renamed["chunks"]]


def test_single_long_token_never_exceeds_chunk_limit_or_loses_characters():
    cfg = config()
    cfg.ingestion.chunk_target_chars = 40
    cfg.ingestion.chunk_max_chars = 60
    cfg.ingestion.chunk_overlap_chars = 10
    result = extract_document(b"x" * 301, "long.txt", "text/plain", cfg)
    chunks = result["chunks"]
    assert all(len(chunk["text"]) <= 60 for chunk in chunks)
    assert chunks[0]["start"] == 0 and chunks[-1]["end"] == 301
    assert all(right["start"] <= left["end"] for left, right in zip(chunks, chunks[1:]))


@pytest.mark.parametrize(("raw", "name", "mime", "code"), [
    (b"hello", "archive.zip", "application/zip", "unsupported_document"),
    (b"hello", "wrong.pdf", "application/pdf", "invalid_document"),
    (b"%PDF-1.3", "wrong.txt", "text/plain", "invalid_document"),
    (b"", "empty.txt", "text/plain", "invalid_document"),
])
def test_admission_rejects_type_mismatch(raw, name, mime, code):
    with pytest.raises(ProviderError) as caught:
        admit_document(raw, name, mime, config())
    assert caught.value.status == code


def test_invalid_encoding_binary_and_whitespace_do_not_activate():
    cfg = config()
    assert extract_document(b"\xffbad", "bad.txt", "text/plain", cfg)["state"] == "failed"
    assert extract_document(b"word\x00word", "bad.txt", "text/plain", cfg)["state"] == "needs_review"
    assert extract_document(b" \n\t", "empty.txt", "text/plain", cfg)["state"] == "needs_review"


def test_upload_and_extraction_limits_are_explicit():
    cfg = config()
    cfg.ingestion.max_upload_bytes = 5
    with pytest.raises(ProviderError, match="upload limit"):
        admit_document(b"123456", "x.txt", "text/plain", cfg)
    cfg.ingestion.max_upload_bytes = 1000
    cfg.ingestion.max_page_extracted_chars = 10
    result = extract_document(b"This is a long document", "x.txt", "text/plain", cfg)
    assert result["state"] == "needs_review"
    assert result["pages"][0]["preview_truncated"] is True
    assert result["chunks"] == []


@pytest.mark.parametrize(("kinds", "state", "page_states"), [
    (["text", "blank"], "extracted", ["extracted", "blank"]),
    (["image"], "needs_ocr", ["needs_ocr"]),
    (["text", "image"], "needs_review", ["extracted", "needs_ocr"]),
    (["text-image"], "needs_review", ["needs_review"]),
    (["drawing"], "needs_review", ["needs_review"]),
    (["blank"], "needs_review", ["blank"]),
])
def test_pdf_coverage_distinguishes_blank_ocr_and_partial_pages(kinds, state, page_states):
    result = extract_document(make_pdf(kinds), "source.pdf", "application/pdf", config())
    assert result["state"] == state
    assert [page["state"] for page in result["pages"]] == page_states


def test_pdf_page_limit_and_corruption_fail_without_partial_activation():
    cfg = config()
    cfg.ingestion.max_pdf_pages = 1
    result = extract_document(make_pdf(["text", "text"]), "source.pdf", "application/pdf", cfg)
    assert result["state"] == "failed"
    assert result["chunks"] == []
    assert extract_document(b"%PDF-1.5\ncorrupt", "source.pdf", "application/pdf", cfg)["state"] == "failed"


@pytest.mark.parametrize("vectors", [[], [[1.0, 2.0]], [[0.0, 0.0, 0.0]], [[float("nan"), 1.0, 0.0]], [[float("inf"), 1.0, 0.0]], [[True, 1.0, 0.0]]])
def test_bad_vectors_are_rejected_before_publication(vectors):
    with pytest.raises(ProviderError):
        validate_vectors(vectors, 1, 3)


class MemoryIngestionStore:
    """Unit-test fake only; application Store always uses PostgreSQL."""
    def __init__(self, cfg, raw):
        self.manifest = space_manifest(cfg)
        self.version = {"version_id": "v1", "document_id": "d1", "corpus_id": "default", "name": "source.txt", "media_type": "text/plain", "raw": raw, "state": "queued", "pages": [], "pipeline_revision": pipeline_revision(cfg)}
        self.chunks = []
        self.cache = {}
        self.vectors = {}
        self.active = False
        self.token = "lease-current"
        self.extraction_count = 0
        self.write_count = 0

    def ensure_corpus(self, corpus_id, manifest):
        if manifest != self.manifest:
            raise ProviderError("space_changed", "Space mismatch")
        return {"id": corpus_id, "space_id": manifest["id"], "revision": 0}

    def get_version(self, version_id, include_bytes=False):
        return deepcopy(self.version)

    def fence(self, lease):
        if lease["token"] != self.token:
            raise ProviderError("lease_lost", "Lease lost")

    def save_extraction(self, version_id, pages, chunks, state, lease):
        self.fence(lease)
        self.version.update(state=state, pages=deepcopy(pages))
        self.chunks = deepcopy(chunks)
        self.extraction_count += 1

    def chunks_for_version(self, version_id):
        return deepcopy(self.chunks)

    def get_cached_embeddings(self, space_id, hashes):
        return {digest: self.cache[digest] for digest in hashes if digest in self.cache}

    def write_embeddings(self, version_id, space_id, vectors, lease):
        self.fence(lease)
        self.vectors.update(deepcopy(vectors))
        by_id = {chunk["id"]: chunk for chunk in self.chunks}
        for chunk_id, vector in vectors.items():
            self.cache[by_id[chunk_id]["text_hash"]] = deepcopy(vector)
        self.write_count += 1

    def activate_version(self, version_id, space_id, lease):
        self.fence(lease)
        assert set(self.vectors) == {chunk["id"] for chunk in self.chunks}
        self.version["state"] = "ready"
        self.active = True


class EmbeddingHub:
    def __init__(self, fail_call=None, lose_lease_store=None):
        self.calls = []
        self.fail_call = fail_call
        self.lose_lease_store = lose_lease_store

    async def embed(self, texts, ctx):
        self.calls.append(texts)
        if self.fail_call == len(self.calls):
            raise ProviderError("provider_unavailable", "Fixture outage")
        if self.lose_lease_store is not None:
            self.lose_lease_store.token = "new-owner"
        return [[1.0, float(len(text)), 1.0] for text in texts]


def test_partial_embedding_failure_resumes_saved_batches_without_reextracting():
    cfg = config()
    cfg.ingestion.chunk_target_chars = 35
    cfg.ingestion.chunk_max_chars = 50
    cfg.ingestion.chunk_overlap_chars = 5
    raw = " ".join(f"Record {i} has a distinct value {i * 7}." for i in range(12)).encode()
    store = MemoryIngestionStore(cfg, raw)
    job = {"id": "job1", "token": store.token, "payload": {"version_id": "v1"}}
    broken = EmbeddingHub(fail_call=2)
    with pytest.raises(ProviderError):
        asyncio.run(ingest_job(job, store, broken, cfg))
    cached_before = set(store.cache)
    assert cached_before and not store.active
    resumed = EmbeddingHub()
    result = asyncio.run(ingest_job(job, store, resumed, cfg))
    assert store.active and result["status"] == "ready"
    assert store.extraction_count == 1
    assert all(text_hash(text) not in cached_before for batch in resumed.calls for text in batch)


def test_lost_lease_after_remote_response_never_publishes_or_activates():
    cfg = config()
    store = MemoryIngestionStore(cfg, b"Keep this source version.")
    job = {"id": "job1", "token": store.token, "payload": {"version_id": "v1"}}
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ingest_job(job, store, EmbeddingHub(lose_lease_store=store), cfg))
    assert caught.value.status == "lease_lost"
    assert store.write_count == 0 and not store.active


def test_review_state_makes_no_embedding_calls():
    cfg = config()
    store = MemoryIngestionStore(cfg, b"binary\x00content")
    hub = EmbeddingHub()
    job = {"id": "job1", "token": store.token, "payload": {"version_id": "v1"}}
    result = asyncio.run(ingest_job(job, store, hub, cfg))
    assert result["status"] == "needs_review"
    assert hub.calls == [] and not store.active


def test_embedding_batch_size_and_total_input_budget_are_bounded():
    cfg = config()
    cfg.ingestion.chunk_target_chars = 70
    cfg.ingestion.chunk_max_chars = 85
    cfg.ingestion.chunk_overlap_chars = 0
    cfg.profiles["e"].max_batch_input_tokens = 250
    store = MemoryIngestionStore(cfg, " ".join(f"Clause {i} contains value {i}, which does not change until January. " for i in range(10)).encode())
    hub = EmbeddingHub()
    result = asyncio.run(ingest_job({"id": "job1", "token": store.token, "payload": {"version_id": "v1"}}, store, hub, cfg))
    assert result["status"] == "ready"
    assert len(hub.calls) > 1
    assert all(len(batch) == 1 for batch in hub.calls)


@pytest.mark.integration
def test_postgres_mock_upload_retrieval_update_and_cited_verdict_round_trip():
    """Exercise actual SQL/pgvector plus fixtures, not model-quality claims."""
    dsn = os.environ.get("RAG_TEST_DSN")
    if not dsn:
        pytest.skip("RAG_TEST_DSN is required for PostgreSQL/pgvector integration")
    from rag_poc.config import load_config
    from rag_poc.domain import CallContext
    from rag_poc.providers import ProviderHub
    from rag_poc.retrieval import retrieve_evidence_variants
    from rag_poc.storage import Store

    cfg = load_config("configs/mock.yaml")
    store = Store(dsn, cfg.ingestion.model_dump())
    store.migrate()
    corpus_id = "ingestion-integration-" + uuid.uuid4().hex
    store.ensure_corpus(corpus_id, space_manifest(cfg))
    # The embedding cache is correctly shared across corpora in the same space.
    # Give this ledger assertion a unique exact input even in the full suite.
    raw_source = f"Employees receive 25 days of annual leave. Source reference {corpus_id}.".encode()
    raw_update = f"Employees receive 30 days of annual leave. Source reference {corpus_id}.".encode()
    upload = store.create_document("leave-policy.txt", raw_source, "text/plain", corpus_id, pipeline_revision=pipeline_revision(cfg))
    duplicate = store.create_document("renamed-policy.txt", raw_source, "text/plain", corpus_id, pipeline_revision=pipeline_revision(cfg))
    assert duplicate["duplicate"] is True and duplicate["version_id"] == upload["version_id"]

    async def round_trip():
        hub = ProviderHub(cfg, store=store)
        try:
            first_job = store.claim_job("ingestion-integration", 120)
            assert first_job["id"] == upload["job_id"]
            result = await ingest_job(first_job, store, hub, cfg)
            assert result["status"] == "ready"
            store.finish_job(first_job["id"], first_job["token"], "succeeded", result=result)
            ctx = CallContext.for_seconds("integration-query-" + uuid.uuid4().hex, "queries")
            question = "How many days of annual leave do employees receive?"
            variants = await retrieve_evidence_variants(question, corpus_id, store, hub, cfg, ctx)
            pack = variants["fused"]
            assert pack.items and all(item.version_id == upload["version_id"] for item in pack.items)
            assert variants["dense"].items
            # websearch_to_tsquery can make a full natural-language question
            # restrictive; the dense branch must remain usable in that case.
            keyword_variants = await retrieve_evidence_variants("annual leave", corpus_id, store, hub, cfg, ctx)
            assert keyword_variants["lexical"].items
            before = pack.content_hash
            draft = await hub.generate(question, pack, ctx)
            verdict = await hub.verify(question, draft, pack, ctx)
            assert verdict.execution_status == "ok"
            assert verdict.answer_hash == draft.content_hash and verdict.evidence_hash == before
            assert any(check.support_status == "supported" for check in verdict.checks)

            updated = store.create_document("leave-policy.txt", raw_update, "text/plain", corpus_id, document_id=upload["document_id"], pipeline_revision=pipeline_revision(cfg))
            second_job = store.claim_job("ingestion-integration", 120)
            assert second_job["id"] == updated["job_id"]
            result = await ingest_job(second_job, store, hub, cfg)
            store.finish_job(second_job["id"], second_job["token"], "succeeded", result=result)
            fresh_ctx = CallContext.for_seconds("integration-query-" + uuid.uuid4().hex, "queries")
            fresh = (await retrieve_evidence_variants(question, corpus_id, store, hub, cfg, fresh_ctx))["fused"]
            assert all(item.version_id == updated["version_id"] for item in fresh.items)
            assert fresh.corpus_revision > pack.corpus_revision
            assert pack.content_hash == before and "25 days" in pack.items[0].text
            assert store.get_version(upload["version_id"], include_bytes=True)["raw"] == raw_source
            assert store.get_calls(first_job["id"])
        finally:
            await hub.aclose()

    asyncio.run(round_trip())
