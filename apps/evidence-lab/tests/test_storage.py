"""Persistence contracts against PostgreSQL and pgvector, never SQLite/fakes.

Set RAG_TEST_DSN to a test PostgreSQL instance. Native runs create a private
schema and only clear that schema's tables. RAG_TEST_BACKEND=pglite requires the
runner's fresh ephemeral database because its socket server ignores search_path
startup options; that branch clears its disposable public schema's RAG tables.
PGlite exercises SQL/pgvector behavior but skips native multi-session tests.
Passing those SQL tests does not establish native concurrency or backup/restore.
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import Barrier, Event

import psycopg
import pytest
from psycopg import sql

from evidence_lab.storage import StorageError, Store, _vector


TABLES = (
    "rag_calls", "rag_run_events", "rag_runs", "rag_jobs", "rag_embedding_cache",
    "rag_chunk_embeddings", "rag_chunks", "rag_document_versions", "rag_documents",
    "rag_corpora", "rag_embedding_spaces",
)
SPACE = {"id": "test-space", "dimensions": 3, "model": "fixture", "fingerprint": "fixture-v1"}




@pytest.fixture
def store(storage_dsn):
    with psycopg.connect(storage_dsn) as connection:
        connection.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(",").join(sql.Identifier(table) for table in TABLES)
            )
        )
    value = Store(storage_dsn)
    value.ensure_corpus("test", SPACE)
    return value


def _expire_job(dsn, job):
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE rag_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job["id"],),
        )


def _chunks(version, texts):
    result, offset = [], 0
    for index, text in enumerate(texts):
        result.append({
            "id": f"{version}:chunk:{index}", "text": text, "page": 1,
            "start": offset, "end": offset + len(text),
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
        offset += len(text) + 2
    return result


def _stage(store, texts, *, corpus="test", document_id=None, vectors=None, activate=False, finish=False):
    raw = "\n\n".join(texts).encode("utf-8")
    document = store.create_document("notes.txt", raw, "text/plain", corpus, document_id=document_id)
    job = store.claim_job("test-worker")
    assert job["id"] == document["job_id"]
    chunks = _chunks(document["version_id"], texts)
    pages = [{"page": 1, "text": raw.decode("utf-8"), "state": "ok"}]
    store.save_extraction(document["version_id"], pages, chunks, "extracted", job)
    if vectors is not None:
        store.write_embeddings(document["version_id"], SPACE["id"], {
            chunk["id"]: vector for chunk, vector in zip(chunks, vectors, strict=True)
        }, job)
    if activate:
        store.activate_version(document["version_id"], SPACE["id"], job)
    if finish:
        store.finish_job(job["id"], job["token"], "succeeded")
    return document, job, chunks, pages


def _call_limits(**changes):
    return {
        "total_cap": 1.0, "phase_caps": {"queries": 1.0, "ingestion": 1.0, "smoke": 1.0, "evaluation": 1.0},
        "run_attempt_cap": 10, "remote_concurrency": 4, "mock": False, "timeout_seconds": 60,
        **changes,
    }


@pytest.mark.parametrize("values", [[], [0, 0, 0], [1, 2], [True, 0, 0], [float("nan"), 1, 0], [float("inf"), 1, 0], [1e100, 0, 1], [1e-100, 0, 0]])
def test_invalid_or_float32_zero_embeddings_are_rejected_before_storage(values):
    with pytest.raises(StorageError) as error:
        _vector(values, 3)
    assert error.value.code == "invalid_embedding"


def test_finite_nonzero_embedding_is_float32_compatible():
    result = _vector([0.1, -0.5, 1], 3)
    assert result == pytest.approx([0.1, -0.5, 1], rel=1e-6)


@pytest.mark.integration
class TestPostgresPersistence:
    def test_migration_is_repeatable_and_pgvector_schema_is_live(self, store):
        store.migrate()
        assert store.health() is True
        assert store.get_corpus("test")["dimensions"] == 3

    def test_upload_is_idempotent_by_bytes_and_pipeline_with_atomic_job(self, store):
        first = store.create_document("original.txt", b"Revenue was 42.", "text/plain", "test")
        repeated = store.create_document("renamed.txt", b"Revenue was 42.", "text/plain", "test")
        assert repeated["duplicate"] is True
        assert repeated["document_id"] == first["document_id"]
        assert repeated["version_id"] == first["version_id"]
        assert repeated["job_id"] == first["job_id"]
        assert len(store.list_documents("test")) == 1
        queued = store.get_job(first["job_id"])
        assert queued["kind"] == "ingest"
        assert queued["payload"] == {"version_id": first["version_id"], "corpus_id": "test"}
        source = store.get_version(first["version_id"], include_bytes=True)
        assert source["raw"] == b"Revenue was 42."
        assert "raw" not in store.get_version(first["version_id"])
        revised = store.create_document("original.txt", b"Revenue was 42.", "text/plain", "test", pipeline_revision="text-v2")
        assert revised["duplicate"] is False
        assert revised["job_id"] != first["job_id"]

    def test_staged_embedding_batches_resume_after_reclaim_without_losing_cache(self, store, storage_dsn):
        document, job, chunks, pages = _stage(store, ["First fact.", "Second fact."])
        store.write_embeddings(document["version_id"], SPACE["id"], {chunks[0]["id"]: [1, 0, 0]}, job)
        with pytest.raises(StorageError) as error:
            store.activate_version(document["version_id"], SPACE["id"], job)
        assert error.value.code == "incomplete_embeddings"
        assert store.get_version(document["version_id"])["active_version_id"] is None
        _expire_job(storage_dsn, job)
        reopened = Store(storage_dsn)
        lease = reopened.claim_job("replacement-worker")
        assert lease["id"] == job["id"] and lease["token"] != job["token"]
        assert lease["attempts"] == 2
        reopened.save_extraction(document["version_id"], pages, chunks, "extracted", lease)
        assert [c["embedded"] for c in reopened.chunks_for_version(document["version_id"])] == [True, False]
        assert reopened.get_cached_embeddings(SPACE["id"], [c["text_hash"] for c in chunks]) == {chunks[0]["text_hash"]: [1, 0, 0]}
        reopened.write_embeddings(document["version_id"], SPACE["id"], {chunks[1]["id"]: [0, 1, 0]}, lease)
        activated = reopened.activate_version(document["version_id"], SPACE["id"], lease)
        repeated = reopened.activate_version(document["version_id"], SPACE["id"], lease)
        assert activated == repeated
        assert activated["corpus_revision"] == 1
        assert reopened.get_version(document["version_id"])["state"] == "ready"

    def test_late_source_worker_cannot_write_renew_or_finish_after_reclaim(self, store, storage_dsn):
        document, old, chunks, pages = _stage(store, ["Lease-owned fact."])
        _expire_job(storage_dsn, old)
        current = store.claim_job("new-worker")
        assert not store.renew_job(old["id"], old["token"])
        actions = [
            lambda: store.save_extraction(document["version_id"], pages, chunks, "extracted", old),
            lambda: store.write_embeddings(document["version_id"], SPACE["id"], {chunks[0]["id"]: [1, 0, 0]}, old),
            lambda: store.activate_version(document["version_id"], SPACE["id"], old),
            lambda: store.finish_job(old["id"], old["token"], "succeeded"),
        ]
        for action in actions:
            with pytest.raises(StorageError) as error:
                action()
            assert error.value.code == "lease_lost"
        assert store.renew_job(current["id"], current["token"])
        assert store.get_job(current["id"])["status"] == "running"

    def test_latest_requested_version_wins_even_if_older_embeddings_finish_first(self, store):
        first, first_job, _, _ = _stage(store, ["Old recommended dose: 5 mg."], vectors=[[1, 0, 0]])
        newer, newer_job, _, _ = _stage(store, ["New recommended dose: 10 mg."], document_id=first["document_id"], vectors=[[0, 1, 0]])
        with pytest.raises(StorageError) as error:
            store.activate_version(first["version_id"], SPACE["id"], first_job)
        assert error.value.code == "superseded"
        store.activate_version(newer["version_id"], SPACE["id"], newer_job)
        assert store.get_version(first["version_id"])["active_version_id"] == newer["version_id"]
        assert store.get_corpus("test")["revision"] == 1

    def test_failed_new_version_leaves_previous_active_retrieval_available(self, store):
        first, _, chunks, _ = _stage(store, ["The active source says blue."], vectors=[[1, 0, 0]], activate=True, finish=True)
        newer = store.create_document("next.txt", b"Unreadable next version", "text/plain", "test", document_id=first["document_id"])
        job = store.claim_job("parser")
        store.finish_job(job["id"], job["token"], "failed", error={"code": "parse_failed"})
        result = store.retrieve("test", SPACE["id"], "blue", [1, 0, 0])
        assert result["dense"][0]["id"] == chunks[0]["id"]
        assert result["lexical"][0]["version_id"] == first["version_id"]
        assert store.get_version(newer["version_id"])["state"] == "failed"

    def test_extracted_content_vectors_and_embedding_space_are_immutable(self, store):
        document, job, chunks, pages = _stage(store, ["Immutable fact."], vectors=[[0.1, 0.2, 0.3]], activate=True)
        with pytest.raises(StorageError) as error:
            store.write_embeddings(document["version_id"], SPACE["id"], {chunks[0]["id"]: [1, 0, 0]}, job)
        assert error.value.code == "embedding_conflict"
        modified = _chunks(document["version_id"], ["Changed fact."])
        modified_pages = [{"page": 1, "text": "Changed fact.", "state": "ok"}]
        with pytest.raises(StorageError) as error:
            store.save_extraction(document["version_id"], modified_pages, modified, "extracted", job)
        assert error.value.code == "source_conflict"
        with pytest.raises(StorageError) as error:
            store.ensure_corpus("test", {**SPACE, "fingerprint": "different-model"})
        assert error.value.code == "space_changed"
        assert store.get_version(document["version_id"])["state"] == "ready"

    def test_invalid_embedding_batch_rolls_back_every_vector(self, store):
        document, job, chunks, _ = _stage(store, ["First entry.", "Second entry."])
        invalid = {chunks[0]["id"]: [1, 0, 0], chunks[1]["id"]: [0, 0, 0]}
        with pytest.raises(StorageError) as error:
            store.write_embeddings(document["version_id"], SPACE["id"], invalid, job)
        assert error.value.code == "invalid_embedding"
        assert not any(c["embedded"] for c in store.chunks_for_version(document["version_id"]))
        assert store.get_cached_embeddings(SPACE["id"], [c["text_hash"] for c in chunks]) == {}

    def test_foreign_chunk_and_foreign_lease_cannot_write_other_source(self, store):
        first, lease1, chunks1, _ = _stage(store, ["Owned by worker one."])
        second, lease2, chunks2, pages2 = _stage(store, ["Owned by worker two."])
        with pytest.raises(StorageError) as error:
            store.write_embeddings(first["version_id"], SPACE["id"], {chunks2[0]["id"]: [1, 0, 0]}, lease1)
        assert error.value.code == "invalid_embedding"
        with pytest.raises(StorageError) as error:
            store.save_extraction(second["version_id"], pages2, chunks2, "extracted", lease1)
        assert error.value.code == "lease_lost"
        assert store.get_job(lease2["id"])["status"] == "running"

    def test_dense_and_native_fts_share_active_source_and_corpus_filters(self, store):
        first, _, old_chunks, _ = _stage(store, ["Running runners observed a blue fox."], vectors=[[1, 0, 0]], activate=True, finish=True)
        new, _, new_chunks, _ = _stage(store, ["Running runners observed a green fox."], document_id=first["document_id"], vectors=[[0.9, 0.1, 0]], activate=True, finish=True)
        pending, _, pending_chunks, _ = _stage(store, ["A running fox in an unactivated source."], vectors=[[1, 0, 0]])
        store.ensure_corpus("foreign", SPACE)
        _, _, foreign_chunks, _ = _stage(store, ["A running fox from another corpus."], corpus="foreign", vectors=[[1, 0, 0]], activate=True, finish=True)
        result = store.retrieve("test", SPACE["id"], "run", [1, 0, 0])
        assert result["corpus_revision"] == 2
        for branch in ("dense", "lexical"):
            assert [row["id"] for row in result[branch]] == [new_chunks[0]["id"]]
            assert result[branch][0]["version_id"] == new["version_id"]
            assert result[branch][0]["rank"] == 1
            assert result[branch][0]["id"] not in {old_chunks[0]["id"], pending_chunks[0]["id"], foreign_chunks[0]["id"]}
        assert store.get_version(pending["version_id"])["active_version_id"] is None
        with pytest.raises(StorageError) as error:
            store.retrieve("test", "changed-space", "run", [1, 0, 0])
        assert error.value.code == "space_changed"

    def test_empty_lexical_branch_and_disabled_dense_branch_are_valid(self, store):
        _, _, chunks, _ = _stage(store, ["A document about finance."], vectors=[[1, 0, 0]], activate=True)
        result = store.retrieve("test", SPACE["id"], "unfindablewordzz", [1, 0, 0])
        assert result["lexical"] == [] and result["dense"][0]["id"] == chunks[0]["id"]
        result = store.retrieve("test", SPACE["id"], "finance", [1, 0, 0], dense_limit=0)
        assert result["dense"] == [] and result["lexical"][0]["id"] == chunks[0]["id"]

    def test_review_state_never_activates_even_with_complete_vectors(self, store):
        document, job, chunks, pages = _stage(store, ["Readable part of a partially unreadable PDF."], vectors=[[1, 0, 0]])
        store.save_extraction(document["version_id"], pages, chunks, "needs_review", job)
        with pytest.raises(StorageError) as error:
            store.activate_version(document["version_id"], SPACE["id"], job)
        assert error.value.code == "invalid_state"
        assert store.get_version(document["version_id"])["active_version_id"] is None

    def test_chunk_coordinates_must_select_the_exact_persisted_page_text(self, store):
        document = store.create_document("coordinates.txt", b"The dose is 10 mg.", "text/plain", "test")
        job = store.claim_job("extractor")
        chunks = _chunks(document["version_id"], ["The dose is 10 mg."])
        chunks[0]["end"] -= 1
        with pytest.raises(StorageError) as error:
            store.save_extraction(
                document["version_id"], [{"page": 1, "text": "The dose is 10 mg."}],
                chunks, "extracted", job,
            )
        assert error.value.code == "invalid_data"
        assert store.chunks_for_version(document["version_id"]) == []
        assert store.get_version(document["version_id"])["state"] == "queued"

    def test_returned_timestamps_are_json_strings_with_utc_offsets(self, store):
        document = store.create_document("clock.txt", b"Timestamp fixture.", "text/plain", "test")
        job = store.claim_job("timestamp-worker")
        source = store.get_version(document["version_id"])
        for value in (source["created_at"], source["updated_at"], job["created_at"], job["updated_at"], job["lease_until"]):
            assert isinstance(value, str)
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        json.dumps({"source": source, "job": job}, allow_nan=False)

    def test_answers_need_owned_active_lease_and_terminal_publication_is_immutable(self, store):
        run = store.create_run("What is known?", "test")
        with pytest.raises(StorageError) as error:
            store.update_run(run["id"], {"status": "answered", "answer": "Unchecked."})
        assert error.value.code == "lease_lost"
        job = store.claim_job("query-worker")
        assert job["payload"]["run_id"] == run["id"]
        with pytest.raises(StorageError) as error:
            store.update_run(run["id"], {"status": "verifying", "answer": "Premature."}, lease=job)
        assert error.value.code == "invalid_state"
        assert store.get_run(run["id"])["answer"] is None
        store.update_run(run["id"], {"status": "answered", "answer": "Verified exact text.", "qualification": "fixture_only"}, lease=job)
        with pytest.raises(StorageError) as error:
            store.update_run(run["id"], {"status": "answered", "answer": "Different text."}, lease=job)
        assert error.value.code == "run_terminal"
        assert store.get_run(run["id"])["answer"] == "Verified exact text."

    def test_query_cancellation_fences_late_publication_events_and_future_calls(self, store):
        run = store.create_run("Cancel this request.", "test")
        job = store.claim_job("query-worker")
        store.update_run(run["id"], {"status": "verifying"}, lease=job)
        store.cancel_job(job["id"])
        assert store.get_run(run["id"])["status"] == "cancelled"
        assert store.get_run(run["id"])["answer"] is None
        for action in (
            lambda: store.update_run(run["id"], {"status": "answered", "answer": "Too late."}, lease=job),
            lambda: store.append_event(run["id"], {"message": "Too late."}, lease=job),
        ):
            with pytest.raises(StorageError) as error:
                action()
            assert error.value.code == "lease_lost"
        with pytest.raises(StorageError) as error:
            store.reserve_call(run["id"], "queries", "test-model", 0.1, _call_limits())
        assert error.value.code == "cancelled"

    def test_cancelled_direct_ingestion_job_cannot_reserve_more_calls(self, store):
        document = store.create_document("cancel.txt", b"Cancel before embedding", "text/plain", "test")
        job = store.claim_job("ingest-worker")
        assert job["id"] == document["job_id"]
        store.cancel_job(job["id"])
        with pytest.raises(StorageError) as error:
            store.reserve_call(job["id"], "ingestion", "embedding-model", 0.01, _call_limits())
        assert error.value.code == "cancelled"

    def test_metadata_credentials_are_redacted_in_settings_events_and_call_records(self, store):
        run = store.create_run("A safe question.", "test", settings={"api_key": "settings-secret", "nested": {"authorization": "Bearer nested-secret"}})
        lease = store.claim_job("worker")
        store.append_event(run["id"], {"api_key": "event-secret", "headers": {"X-Key": "header-secret"}}, lease=lease)
        call = store.reserve_call(run["id"], "queries", "test-model", 0.1, _call_limits())
        store.finish_call(call, "failed", detail={"password": "call-secret", "headers": {"Authorization": "bearer-secret"}})
        serialized = json.dumps({"run": store.get_run(run["id"]), "calls": store.get_calls(run["id"])})
        assert all(secret not in serialized for secret in ("settings-secret", "nested-secret", "event-secret", "header-secret", "call-secret", "bearer-secret"))

    def test_upload_and_document_quotas_fail_without_partial_jobs(self, store, storage_dsn):
        limited = Store(storage_dsn, {"max_upload_bytes": 10, "max_documents": 1})
        with pytest.raises(StorageError) as error:
            limited.create_document("oversized.txt", b"this upload exceeds ten bytes", "text/plain", "test")
        assert error.value.code == "quota_exceeded"
        assert limited.list_jobs() == []
        limited.create_document("first.txt", b"first", "text/plain", "test")
        with pytest.raises(StorageError) as error:
            limited.create_document("second.txt", b"second", "text/plain", "test")
        assert error.value.code == "quota_exceeded"
        assert len(limited.list_documents("test")) == 1
        assert len(limited.list_jobs()) == 1

    def test_global_and_phase_caps_and_disabled_scopes_block_calls(self, store):
        for limits in (_call_limits(total_cap=0), _call_limits(phase_caps={"queries": 0})):
            with pytest.raises(StorageError) as error:
                store.reserve_call("run-1", "queries", "test-model", 0, limits)
            assert error.value.code == "budget_exhausted"
        limits = _call_limits(total_cap=0.15, phase_caps={"queries": 0.1, "ingestion": 0.2})
        first = store.reserve_call("run-1", "queries", "test-model", 0.08, limits)
        store.finish_call(first, "timeout")
        with pytest.raises(StorageError) as error:
            store.reserve_call("run-2", "queries", "test-model", 0.03, limits)
        assert error.value.code == "budget_exhausted"  # phase cap
        with pytest.raises(StorageError) as error:
            store.reserve_call("ingest-1", "ingestion", "embedding-model", 0.08, limits)
        assert error.value.code == "budget_exhausted"  # total cap
        assert len(store.get_calls()) == 1

    def test_unknown_cost_and_attempt_caps_survive_store_recreation(self, store, storage_dsn):
        limits = _call_limits(total_cap=0.15, run_attempt_cap=1)
        call = store.reserve_call("persistent-run", "queries", "test-model", 0.1, limits)
        store.finish_call(call, "timeout", usage=None, actual_cost=None)
        reopened = Store(storage_dsn)
        recorded = reopened.get_calls("persistent-run")[0]
        assert recorded["charged_cost"] == 0.1
        assert recorded["actual_cost"] is None and recorded["cost_known"] is False
        with pytest.raises(StorageError) as error:
            reopened.reserve_call("persistent-run", "queries", "test-model", 0.01, limits)
        assert error.value.code == "budget_exhausted"
        with pytest.raises(StorageError) as error:
            reopened.reserve_call("new-run", "queries", "test-model", 0.06, limits)
        assert error.value.code == "budget_exhausted"

    def test_active_global_concurrency_releases_on_completion_and_expiration(self, store, storage_dsn):
        limits = _call_limits(remote_concurrency=1)
        call = store.reserve_call("run-1", "queries", "first-profile", 0.1, limits)
        with pytest.raises(StorageError) as error:
            store.reserve_call("run-2", "queries", "different-profile", 0.1, limits)
        assert error.value.code == "concurrency_limited" and error.value.retryable
        with psycopg.connect(storage_dsn) as connection:
            connection.execute("UPDATE rag_calls SET active_until=clock_timestamp()-interval '1 second' WHERE id=%s", (call,))
        next_call = store.reserve_call("run-2", "queries", "different-profile", 0.1, limits)
        expired = store.get_calls("run-1")[0]
        assert expired["status"] == "expired_unknown" and expired["charged_cost"] == 0.1
        store.finish_call(next_call, "ok", actual_cost=0.04)
        assert store.reserve_call("run-3", "queries", "third-profile", 0.1, limits)

    def test_known_charge_cannot_be_rewritten_and_overestimate_is_flagged(self, store):
        limits = _call_limits()
        call = store.reserve_call("cost-run", "queries", "test-model", 0.01, limits)
        completed = store.finish_call(call, "ok", usage={"input_tokens": 100}, actual_cost=0.02)
        assert completed["charged_cost"] == 0.02 and completed["cost_known"]
        assert completed["detail"]["estimate_exceeded"] is True
        store.finish_call(call, "ok", actual_cost=0.02)
        with pytest.raises(StorageError) as error:
            store.finish_call(call, "ok", actual_cost=0)
        assert error.value.code == "invalid_data"
        assert store.get_calls("cost-run")[0]["charged_cost"] == 0.02

    def test_profile_concurrency_is_shared_across_store_instances_and_releases_on_completion(self, store, storage_dsn):
        other_worker = Store(storage_dsn)
        limits = _call_limits(remote_concurrency=3, profile_concurrency=1)
        first = store.reserve_call("profile-run-1", "queries", "profile-a", 0.01, limits)
        with pytest.raises(StorageError) as error:
            other_worker.reserve_call("profile-run-2", "queries", "profile-a", 0.01, limits)
        assert error.value.code == "concurrency_limited" and error.value.retryable is True
        assert other_worker.get_calls("profile-run-2") == []
        other_worker.reserve_call("profile-run-3", "queries", "profile-b", 0.01, limits)
        store.finish_call(first, "timeout")
        released = other_worker.reserve_call("profile-run-2", "queries", "profile-a", 0.01, limits)
        assert released and len(store.get_calls()) == 3
        assert store.budget_summary()["active_calls"] == 2

    def test_mock_ledger_cannot_spend_money_or_reset_attempt_caps(self, store):
        limits = _call_limits(mock=True, total_cap=0, phase_caps={}, run_attempt_cap=1)
        call = store.reserve_call("mock-run", "queries", "fixture", 999, limits)
        store.finish_call(call, "ok", actual_cost=999)
        record = store.get_calls("mock-run")[0]
        assert record["mock"] and record["charged_cost"] == 0 and record["actual_cost"] == 0
        with pytest.raises(StorageError) as error:
            store.reserve_call("mock-run", "queries", "fixture", 0, limits)
        assert error.value.code == "budget_exhausted"

    def test_explicit_source_purge_cancels_holding_query_and_preserves_cost_ledger(self, store):
        document, _, chunks, _ = _stage(store, ["Source content to purge."], vectors=[[1, 0, 0]], activate=True, finish=True)
        run = store.create_run("Use this source.", "test")
        lease = store.claim_job("query")
        store.update_run(run["id"], {"status": "verifying", "evidence": {"version_id": document["version_id"]}}, lease=lease)
        call = store.reserve_call(run["id"], "queries", "test-model", 0.1, _call_limits())
        store.finish_call(call, "timeout")
        deleted = store.delete_document(document["document_id"])
        assert deleted["deleted"] and deleted["purged_runs"] == 1
        assert store.list_documents("test") == []
        assert store.get_job(lease["id"])["status"] == "cancelled"
        with pytest.raises(StorageError):
            store.get_version(document["version_id"], include_bytes=True)
        with pytest.raises(StorageError):
            store.get_run(run["id"])
        assert store.get_cached_embeddings(SPACE["id"], [chunks[0]["text_hash"]]) == {}
        assert store.get_calls(run["id"])[0]["charged_cost"] == 0.1
        with pytest.raises(StorageError) as error:
            store.reserve_call(run["id"], "queries", "test-model", 0.1, _call_limits())
        assert error.value.code == "cancelled"
        with pytest.raises(StorageError) as error:
            store.reserve_call("ingest:" + document["version_id"], "ingestion", "test-model", 0.1, _call_limits())
        assert error.value.code == "cancelled"

    def test_cleanup_preserves_referenced_old_source_then_removes_it_after_run_retention(self, store):
        old, _, _, _ = _stage(
            store, ["The previous source says blue."], vectors=[[1, 0, 0]], activate=True, finish=True,
        )
        active, _, chunks, _ = _stage(
            store, ["The current source says green."], document_id=old["document_id"],
            vectors=[[0, 1, 0]], activate=True, finish=True,
        )
        run = store.create_run("Explain an earlier source snapshot.", "test")
        lease = store.claim_job("retained-query")
        # Exercise the event-reference guard independently of run.data.
        store.append_event(run["id"], {"evidence_version_id": old["version_id"]}, lease=lease)
        result = store.cleanup(inactive_days=0, failed_days=0)
        assert result["deleted_versions"] == 0
        assert store.get_version(old["version_id"])["version_id"] == old["version_id"]
        assert store.get_version(active["version_id"])["active_version_id"] == active["version_id"]
        store.update_run(run["id"], {"status": "abstained", "answer": None}, lease=lease)
        store.finish_job(lease["id"], lease["token"], "succeeded")
        result = store.cleanup(inactive_days=0, failed_days=0)
        assert result["deleted_runs"] == 1 and result["deleted_versions"] == 1
        with pytest.raises(StorageError) as error:
            store.get_version(old["version_id"])
        assert error.value.code == "not_found"
        retrieved = store.retrieve("test", SPACE["id"], "green", [0, 1, 0])
        assert retrieved["dense"][0]["id"] == chunks[0]["id"]
        assert retrieved["lexical"][0]["version_id"] == active["version_id"]


@pytest.mark.integration
@pytest.mark.native_postgres
class TestNativeMultiSessionConcurrency:
    @pytest.fixture(autouse=True)
    def native_only(self):
        if os.environ.get("RAG_TEST_BACKEND", "").lower() == "pglite":
            pytest.skip("PGlite SQL checks do not establish native multi-session concurrency.")

    def test_two_workers_cannot_claim_same_job(self, store, storage_dsn):
        jobs = [store.enqueue_job("evaluation", {"dataset": "fixture"}) for _ in range(2)]
        barrier = Barrier(2)
        def claim(worker):
            barrier.wait(timeout=5)
            return Store(storage_dsn).claim_job(worker)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim, f"worker-{index}") for index in range(2)]
            claimed = [future.result(timeout=15) for future in futures]
        assert {job["id"] for job in claimed} == {job["id"] for job in jobs}
        assert len({job["token"] for job in claimed}) == 2

    def test_competing_reservations_cannot_overrun_global_spend(self, store, storage_dsn):
        limits = _call_limits(total_cap=0.1)
        barrier = Barrier(2)
        def reserve(run):
            barrier.wait(timeout=5)
            try:
                return Store(storage_dsn).reserve_call(run, "queries", "test-model", 0.06, limits)
            except StorageError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, f"race-{index}") for index in range(2)]
            results = [future.result(timeout=15) for future in futures]
        assert results.count("budget_exhausted") == 1
        calls = store.get_calls()
        assert len(calls) == 1 and calls[0]["charged_cost"] == 0.06

    def test_dense_and_lexical_keep_one_snapshot_during_concurrent_activation(self, store, monkeypatch):
        old, _, _, _ = _stage(
            store, ["The source policy allows blue."], vectors=[[1, 0, 0]], activate=True, finish=True,
        )
        new, job, _, _ = _stage(
            store, ["The source policy allows green."], document_id=old["document_id"], vectors=[[0, 1, 0]],
        )
        old_revision = store.get_corpus("test")["revision"]
        dense_executed, activation_completed = Event(), Event()
        original_transaction = store._transaction

        @contextmanager
        def interleaved_transaction(*, readonly=False):
            with original_transaction(readonly=readonly) as connection:
                class ConnectionProxy:
                    count = 0

                    def execute(self, statement, parameters=None):
                        cursor = connection.execute(statement, parameters)
                        self.count += 1
                        # retrieve pins the corpus first and executes dense SQL
                        # second. Pause that real transaction before lexical SQL.
                        if readonly and self.count == 2:
                            dense_executed.set()
                            assert activation_completed.wait(timeout=10), "Concurrent activation did not finish"
                        return cursor

                yield ConnectionProxy()

        monkeypatch.setattr(store, "_transaction", interleaved_transaction)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(store.retrieve, "test", SPACE["id"], "source policy", [1, 0, 0])
            try:
                assert dense_executed.wait(timeout=5), "Dense query did not reach the interleave point"
                store.activate_version(new["version_id"], SPACE["id"], job)
            finally:
                activation_completed.set()
            frozen = future.result(timeout=15)
        assert frozen["corpus_revision"] == old_revision
        assert frozen["dense"] and frozen["lexical"]
        assert {row["version_id"] for branch in ("dense", "lexical") for row in frozen[branch]} == {old["version_id"]}
        fresh = store.retrieve("test", SPACE["id"], "source policy", [0, 1, 0])
        assert fresh["corpus_revision"] > frozen["corpus_revision"]
        assert {row["version_id"] for branch in ("dense", "lexical") for row in fresh[branch]} == {new["version_id"]}
