"""PostgreSQL persistence. No remote inference occurs inside these transactions.

All public results are JSON-safe except the explicitly requested original bytes
from get_version(..., include_bytes=True). Raw database errors never reach callers.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, overload

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from .domain import PIPELINE_VERSION, ProviderError
from .memory import DEFAULT_MEMORY, bounded_turns


TERMINAL_RUNS = {
    "answered", "abstained", "verification_unavailable", "failed", "cancelled", "timed_out", "shadow",
}
DEFAULT_LIMITS = {
    "max_upload_bytes": 20 * 1024 * 1024,
    "max_documents": 100,
    "max_active_chunks": 10000,
    "max_retained_payload_bytes": 5 * 1024**3,
    "inactive_retention_days": 30,
    "failed_staging_retention_days": 7,
}
_BUDGET_LOCK = 814372901
_QUOTA_LOCK = 814372902


class StorageError(ProviderError):
    """Safe, code-bearing storage failure; never embeds SQL or credentials."""

    @property
    def code(self):
        return self.status


def _id() -> str:
    return str(uuid.uuid4())


def _required_row(row: DictRow | None) -> DictRow:
    """Validate a row guaranteed by an aggregate, write or foreign-key relation."""
    if row is None:
        raise StorageError("invalid_data", "The database operation did not return its required row.")
    return row


@overload
def _json_safe(value: dict[Any, Any]) -> dict[str, Any]: ...


@overload
def _json_safe(value: list[Any] | tuple[Any, ...]) -> list[Any]: ...


@overload
def _json_safe(value: Any) -> Any: ...


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise StorageError("invalid_data", "Database output contained a non-finite value.")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, memoryview):
        return bytes(value)
    return value


@overload
def _metadata(value: dict[Any, Any]) -> dict[str, Any]: ...


@overload
def _metadata(value: list[Any] | tuple[Any, ...]) -> list[Any]: ...


@overload
def _metadata(value: Any) -> Any: ...


def _metadata(value: Any) -> Any:
    """Redact credential-shaped metadata fields without changing evidence text."""
    secret_names = {"api_key", "apikey", "authorization", "password", "secret", "token",
                    "auth_headers", "headers", "database_dsn", "dsn"}
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if str(k).lower() in secret_names else _metadata(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_metadata(v) for v in value]
    return _json_safe(value)


def _json(value, *, redact=False):
    value = _metadata(value) if redact else _json_safe(value)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise StorageError("invalid_data", "Persistence data must be finite JSON values.") from None
    return Jsonb(value)


def _vector(values, dimensions):
    if not isinstance(values, (list, tuple)) or len(values) != dimensions:
        raise StorageError("invalid_embedding", "Embedding dimensions do not match the corpus.")
    normalized = []
    try:
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError
            value = float(value)
            if not math.isfinite(value):
                raise ValueError
            rounded = struct.unpack("!f", struct.pack("!f", value))[0]
            if not math.isfinite(rounded):
                raise ValueError
            normalized.append(rounded)
    except (ValueError, OverflowError, struct.error):
        raise StorageError("invalid_embedding", "Embedding contains an invalid numeric value.") from None
    if not any(normalized):
        raise StorageError("invalid_embedding", "Zero vectors cannot be used for cosine retrieval.")
    return normalized


def _vector_literal(values):
    return "[" + ",".join(repr(v) for v in values) + "]"


def _cost(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result >= Decimal("1000000000000"):
            raise ValueError
        return result.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
    except (ValueError, ArithmeticError):
        raise StorageError("budget_exhausted", "A finite nonnegative call cost is required.") from None


class Store:
    def __init__(self, dsn: str, limits: dict | None = None, memory: dict | None = None):
        self._dsn = dsn
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.memory = {**DEFAULT_MEMORY, **(memory or {})}

    @contextmanager
    def _transaction(self, *, readonly=False):
        try:
            with psycopg.Connection[DictRow].connect(
                self._dsn, row_factory=dict_row, connect_timeout=10,
            ) as connection:
                if readonly:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                yield connection
        except StorageError:
            raise
        except psycopg.Error:
            raise StorageError("storage_unavailable", "The database operation could not be completed.",
                               retryable=True) from None

    def migrate(self):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.pool import NullPool
        engine = None
        try:
            url = make_url(self._dsn).set(drivername="postgresql+psycopg")
            engine = create_engine(url, poolclass=NullPool)
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
            with engine.begin() as connection:
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(814372900)")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        except Exception:
            raise StorageError("migration_failed",
                               "Database migrations failed. Check PostgreSQL and pgvector availability.") from None
        finally:
            if engine is not None:
                engine.dispose()

    def health(self) -> bool:
        try:
            with self._transaction(readonly=True) as connection:
                row = _required_row(connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') AS vector, "
                    "to_regclass('evidence_corpora') IS NOT NULL AS schema"
                ).fetchone())
                return bool(row["vector"] and row["schema"])
        except StorageError:
            return False

    def ensure_corpus(self, corpus_id: str, space: dict) -> dict:
        required = ("id", "dimensions", "model", "fingerprint")
        if not corpus_id or any(key not in space for key in required):
            raise StorageError("invalid_data", "Corpus and complete embedding-space settings are required.")
        dimensions = space["dimensions"]
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= 16000:
            raise StorageError("invalid_embedding", "Embedding dimensions must be between 1 and 16000.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            existing = connection.execute("SELECT * FROM evidence_embedding_spaces WHERE id=%s",
                                          (space["id"],)).fetchone()
            if existing and any(existing[key] != space[key] for key in ("dimensions", "model", "fingerprint")):
                raise StorageError("space_changed", "Embedding-space identity cannot be changed in place.")
            if not existing:
                connection.execute(
                    "INSERT INTO evidence_embedding_spaces(id,dimensions,model,fingerprint,manifest) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (space["id"], dimensions, space["model"], space["fingerprint"], _json(space, redact=True)),
                )
            corpus = connection.execute("SELECT * FROM evidence_corpora WHERE id=%s FOR UPDATE",
                                        (corpus_id,)).fetchone()
            if corpus and corpus["space_id"] != space["id"]:
                raise StorageError("space_changed", "This corpus requires its existing embedding space.")
            if not corpus:
                corpus = connection.execute(
                    "INSERT INTO evidence_corpora(id,space_id) VALUES (%s,%s) RETURNING *",
                    (corpus_id, space["id"]),
                ).fetchone()
            return _json_safe(corpus)

    def list_corpora(self) -> list[dict]:
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                "SELECT c.id,c.space_id,c.revision,s.dimensions,s.model "
                "FROM evidence_corpora c JOIN evidence_embedding_spaces s ON s.id=c.space_id ORDER BY c.id"
            ).fetchall()
            return _json_safe(rows)

    def get_corpus(self, corpus_id="default") -> dict:
        with self._transaction(readonly=True) as connection:
            row = connection.execute(
                "SELECT c.*,s.dimensions,s.model,s.fingerprint FROM evidence_corpora c "
                "JOIN evidence_embedding_spaces s ON s.id=c.space_id WHERE c.id=%s", (corpus_id,),
            ).fetchone()
            if not row:
                raise StorageError("not_found", "Corpus not found.")
            return _json_safe(row)

    def _payload_bytes(self, connection):
        return connection.execute(
            "SELECT COALESCE((SELECT sum(octet_length(raw)+octet_length(pages::text)) "
            "FROM evidence_document_versions),0) + "
            "COALESCE((SELECT sum(octet_length(text)) FROM evidence_chunks),0) + "
            "COALESCE((SELECT sum(octet_length(data::text)+octet_length(settings::text)+"
            "octet_length(question)) FROM evidence_runs),0) + "
            "COALESCE((SELECT sum(octet_length(event::text)) FROM evidence_run_events),0) + "
            "COALESCE((SELECT sum(octet_length(COALESCE(result::text,''))) FROM evidence_jobs),0) AS n"
        ).fetchone()["n"]

    def _check_quota(self, connection, additional=0):
        if self._payload_bytes(connection) + additional > self.limits["max_retained_payload_bytes"]:
            raise StorageError("quota_exceeded", "Retained payload quota is exhausted.")

    def _insert_job(self, connection: psycopg.Connection[DictRow], kind, payload) -> DictRow:
        return _required_row(connection.execute(
            "INSERT INTO evidence_jobs(id,kind,payload) VALUES (%s,%s,%s) RETURNING *",
            (_id(), kind, _json(payload)),
        ).fetchone())

    def create_document(self, name, raw: bytes, media_type, corpus_id="default",
                        document_id=None, pipeline_revision=PIPELINE_VERSION) -> dict:
        if not isinstance(raw, bytes) or not raw:
            raise StorageError("invalid_document", "An uploaded document must contain bytes.")
        if len(raw) > self.limits["max_upload_bytes"]:
            raise StorageError("quota_exceeded", "Document exceeds the configured upload limit.")
        if not isinstance(name, str) or not name or len(name) > 1024:
            raise StorageError("invalid_document", "A valid document name is required.")
        content_hash = hashlib.sha256(raw).hexdigest()
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            corpus = connection.execute("SELECT * FROM evidence_corpora WHERE id=%s FOR UPDATE",
                                        (corpus_id,)).fetchone()
            if not corpus:
                raise StorageError("not_found", "Corpus not found.")
            duplicate = connection.execute(
                "SELECT v.id AS version_id,v.document_id,v.job_id,v.state FROM evidence_document_versions v "
                "JOIN evidence_documents d ON d.id=v.document_id WHERE d.corpus_id=%s "
                "AND d.deleted_at IS NULL AND v.version_no=d.latest_version_no AND v.content_hash=%s "
                "AND v.pipeline_revision=%s AND v.space_id=%s "
                "AND (%s::text IS NULL OR d.id=%s) LIMIT 1",
                (corpus_id, content_hash, pipeline_revision, corpus["space_id"], document_id, document_id),
            ).fetchone()
            if duplicate:
                return _json_safe({**duplicate, "duplicate": True, "id": duplicate["document_id"]})
            self._check_quota(connection, len(raw))
            if document_id:
                document = connection.execute(
                    "SELECT * FROM evidence_documents WHERE id=%s AND corpus_id=%s "
                    "AND deleted_at IS NULL FOR UPDATE", (document_id, corpus_id),
                ).fetchone()
                if not document:
                    raise StorageError("not_found", "Document not found.")
            else:
                count = _required_row(connection.execute(
                    "SELECT count(*) AS n FROM evidence_documents WHERE corpus_id=%s AND deleted_at IS NULL",
                    (corpus_id,),
                ).fetchone())["n"]
                if count >= self.limits["max_documents"]:
                    raise StorageError("quota_exceeded", "Corpus document limit reached.")
                document_id = _id()
                document = _required_row(connection.execute(
                    "INSERT INTO evidence_documents(id,corpus_id,name,media_type) VALUES (%s,%s,%s,%s) RETURNING *",
                    (document_id, corpus_id, name, media_type),
                ).fetchone())
            version_id = _id()
            version_no = document["latest_version_no"] + 1
            job = self._insert_job(connection, "ingest", {"version_id": version_id, "corpus_id": corpus_id})
            connection.execute(
                "INSERT INTO evidence_document_versions(id,document_id,version_no,space_id,name,media_type,content_hash,"
                "pipeline_revision,raw,job_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (version_id, document_id, version_no, corpus["space_id"], name, media_type, content_hash,
                 pipeline_revision, raw, job["id"]),
            )
            connection.execute(
                "UPDATE evidence_documents SET latest_version_no=%s,name=%s,media_type=%s,"
                "updated_at=clock_timestamp() WHERE id=%s",
                (version_no, name, media_type, document_id),
            )
            return {"id": document_id, "document_id": document_id, "version_id": version_id,
                    "job_id": job["id"], "duplicate": False, "state": "queued"}

    def get_version(self, version_id, include_bytes=False) -> dict:
        with self._transaction(readonly=True) as connection:
            columns = "v.*" if include_bytes else (
                "v.id,v.document_id,v.version_no,v.space_id,v.name,v.media_type,v.content_hash,v.pipeline_revision,"
                "v.state,v.pages,v.job_id,v.created_at,v.updated_at,v.activated_at"
            )
            row = connection.execute(
                f"SELECT {columns},d.corpus_id,d.active_version_id,"
                "(SELECT count(*) FROM evidence_chunks WHERE version_id=v.id) AS chunk_count,"
                "(SELECT count(*) FROM evidence_chunk_embeddings e JOIN evidence_chunks ch ON ch.id=e.chunk_id "
                "WHERE ch.version_id=v.id AND e.space_id=v.space_id) AS embedded_chunks "
                "FROM evidence_document_versions v JOIN evidence_documents d ON d.id=v.document_id WHERE v.id=%s",
                (version_id,),
            ).fetchone()
            if not row:
                raise StorageError("not_found", "Source version not found.")
            row["version_id"] = row["id"]
            return _json_safe(row)

    def list_documents(self, corpus_id="default") -> list[dict]:
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                "SELECT d.*,v.id AS version_id,v.state,v.job_id,v.content_hash,v.created_at AS version_created_at,"
                "j.status AS job_status,"
                "(SELECT count(*) FROM evidence_chunks WHERE version_id=v.id) AS chunk_count,"
                "(SELECT count(*) FROM evidence_chunk_embeddings e JOIN evidence_chunks ch ON ch.id=e.chunk_id "
                "WHERE ch.version_id=v.id AND e.space_id=v.space_id) AS embedded_chunks "
                "FROM evidence_documents d LEFT JOIN evidence_document_versions v ON v.document_id=d.id "
                "AND v.version_no=d.latest_version_no LEFT JOIN evidence_jobs j ON j.id=v.job_id "
                "WHERE d.corpus_id=%s AND d.deleted_at IS NULL ORDER BY d.created_at,d.id", (corpus_id,),
            ).fetchall()
            return _json_safe([{**row, "document_id": row["id"]} for row in rows])

    def _assert_lease(self, connection, lease, *, version_id=None, run_id=None):
        if not lease or not lease.get("id") or not lease.get("token"):
            raise StorageError("lease_lost", "An active worker lease is required.")
        job = connection.execute(
            "SELECT * FROM evidence_jobs WHERE id=%s AND token=%s AND status='running' "
            "AND lease_until>clock_timestamp() FOR UPDATE", (lease["id"], lease["token"]),
        ).fetchone()
        if not job:
            raise StorageError("lease_lost", "Worker lease has expired or was replaced.")
        if version_id is not None and job["payload"].get("version_id") != version_id:
            raise StorageError("lease_lost", "Worker lease does not own this source version.")
        if run_id is not None and job["payload"].get("run_id") != run_id:
            raise StorageError("lease_lost", "Worker lease does not own this query run.")
        return job

    def _owned_version(self, connection, version_id, lease):
        self._assert_lease(connection, lease, version_id=version_id)
        row = connection.execute(
            "SELECT v.*,d.corpus_id,d.deleted_at,d.latest_version_no,d.active_version_id,"
            "d.active_version_no FROM evidence_document_versions v "
            "JOIN evidence_documents d ON d.id=v.document_id WHERE v.id=%s FOR UPDATE OF v,d",
            (version_id,),
        ).fetchone()
        if not row or row["deleted_at"] is not None:
            raise StorageError("not_found", "Source version is no longer available.")
        return row

    def save_extraction(self, version_id, pages: list[dict], chunks: list[dict],
                        state: str, lease: dict):
        # Only activate_version may publish 'ready'.
        if state == "ready":
            state = "extracted"
        if state not in {"extracted", "needs_review", "needs_ocr", "failed"}:
            raise StorageError("invalid_data", "Unsupported extraction state.")
        if not isinstance(pages, list) or not isinstance(chunks, list):
            raise StorageError("invalid_data", "Extraction pages and chunks must be lists.")
        page_text = {}
        for page in pages:
            if (not isinstance(page, dict) or not isinstance(page.get("page"), int)
                    or isinstance(page.get("page"), bool) or page["page"] < 1
                    or page["page"] in page_text or not isinstance(page.get("text"), str)):
                raise StorageError("invalid_data", "Extraction pages require unique numbers and canonical text.")
            page_text[page["page"]] = page["text"]
        if len(chunks) > self.limits["max_active_chunks"]:
            raise StorageError("quota_exceeded", "Document exceeds the corpus chunk limit.")
        normalized, ids, locations = [], set(), set()
        for chunk in chunks:
            try:
                item = {key: chunk[key] for key in ("id", "text", "page", "start", "end", "text_hash")}
                if not isinstance(item["id"], str) or not item["id"] or item["id"] in ids:
                    raise ValueError
                if not isinstance(item["text"], str) or not item["text"]:
                    raise ValueError
                if any(isinstance(item[key], bool) or not isinstance(item[key], int)
                       for key in ("page", "start", "end")):
                    raise ValueError
                if item["page"] < 1 or item["start"] < 0 or item["end"] < item["start"]:
                    raise ValueError
                if item["end"] - item["start"] != len(item["text"]):
                    raise ValueError
                if page_text.get(item["page"], "")[item["start"]:item["end"]] != item["text"]:
                    raise ValueError
                if item["text_hash"] != hashlib.sha256(item["text"].encode("utf-8")).hexdigest():
                    raise ValueError
                location = (item["page"], item["start"], item["end"])
                if location in locations:
                    raise ValueError
                ids.add(item["id"])
                locations.add(location)
                normalized.append(item)
            except (KeyError, TypeError, ValueError):
                raise StorageError("invalid_data", "A chunk has invalid identity, text, hash, or coordinates.") from None
        _json(pages)
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            version = self._owned_version(connection, version_id, lease)
            existing = connection.execute(
                'SELECT id,text,text_hash,page,start_offset AS "start",end_offset AS "end" '
                "FROM evidence_chunks WHERE version_id=%s ORDER BY id", (version_id,),
            ).fetchall()
            expected = sorted(normalized, key=lambda item: item["id"])
            if existing and existing != expected:
                raise StorageError("source_conflict", "An extracted source version cannot be changed in place.")
            if existing and version["pages"] != pages:
                raise StorageError("source_conflict", "Canonical source pages cannot be changed in place.")
            if version["state"] == "ready":
                if existing != expected or version["pages"] != pages:
                    raise StorageError("source_conflict", "An active source version is immutable.")
                return
            if not existing:
                additional = sum(len(item["text"].encode("utf-8")) for item in normalized)
                additional += len(json.dumps(pages, ensure_ascii=False).encode("utf-8"))
                self._check_quota(connection, additional)
                for item in normalized:
                    connection.execute(
                        "INSERT INTO evidence_chunks(id,version_id,text,text_hash,page,start_offset,end_offset) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (item["id"], version_id, item["text"], item["text_hash"], item["page"],
                         item["start"], item["end"]),
                    )
            connection.execute(
                "UPDATE evidence_document_versions SET pages=%s,state=%s,updated_at=clock_timestamp() WHERE id=%s",
                (_json(pages), state, version_id),
            )

    def chunks_for_version(self, version_id) -> list[dict]:
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                'SELECT ch.id,ch.version_id,ch.text,ch.text_hash,ch.page,ch.start_offset AS "start",'
                'ch.end_offset AS "end",EXISTS(SELECT 1 FROM evidence_chunk_embeddings e '
                "JOIN evidence_document_versions v ON v.id=ch.version_id "
                "WHERE e.chunk_id=ch.id AND e.space_id=v.space_id) AS embedded "
                "FROM evidence_chunks ch WHERE ch.version_id=%s ORDER BY ch.page,ch.start_offset,ch.id",
                (version_id,),
            ).fetchall()
            return _json_safe(rows)

    def get_cached_embeddings(self, space_id, hashes: list[str]) -> dict[str, list[float]]:
        if not hashes:
            return {}
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                "SELECT text_hash,embedding::text AS embedding FROM evidence_embedding_cache "
                "WHERE space_id=%s AND text_hash=ANY(%s)", (space_id, list(set(hashes))),
            ).fetchall()
            return {row["text_hash"]: json.loads(row["embedding"]) for row in rows}

    def write_embeddings(self, version_id, space_id, vectors: dict[str, list[float]], lease: dict):
        if not isinstance(vectors, dict):
            raise StorageError("invalid_embedding", "Embedding batches must be keyed by chunk ID.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            version = self._owned_version(connection, version_id, lease)
            if version["space_id"] != space_id:
                raise StorageError("space_changed", "Embedding space does not match the staged version.")
            space = _required_row(connection.execute("SELECT dimensions FROM evidence_embedding_spaces WHERE id=%s",
                                       (space_id,)).fetchone())
            chunks = connection.execute(
                "SELECT id,text_hash FROM evidence_chunks WHERE version_id=%s AND id=ANY(%s)",
                (version_id, list(vectors)),
            ).fetchall() if vectors else []
            if len(chunks) != len(vectors):
                raise StorageError("invalid_embedding", "Embedding batch contains a foreign or missing chunk.")
            for chunk in chunks:
                vector = _vector(vectors[chunk["id"]], space["dimensions"])
                literal = _vector_literal(vector)
                connection.execute(
                    "INSERT INTO evidence_embedding_cache(space_id,text_hash,embedding) VALUES (%s,%s,%s::vector) "
                    "ON CONFLICT DO NOTHING", (space_id, chunk["text_hash"], literal),
                )
                same = _required_row(connection.execute(
                    "SELECT embedding=%s::vector AS same FROM evidence_embedding_cache "
                    "WHERE space_id=%s AND text_hash=%s", (literal, space_id, chunk["text_hash"]),
                ).fetchone())
                if not same["same"]:
                    raise StorageError("embedding_conflict", "Cached embeddings differ within an immutable space.")
                connection.execute(
                    "INSERT INTO evidence_chunk_embeddings(chunk_id,space_id,embedding) VALUES (%s,%s,%s::vector) "
                    "ON CONFLICT DO NOTHING", (chunk["id"], space_id, literal),
                )
                same = _required_row(connection.execute(
                    "SELECT embedding=%s::vector AS same FROM evidence_chunk_embeddings WHERE chunk_id=%s AND space_id=%s",
                    (literal, chunk["id"], space_id),
                ).fetchone())
                if not same["same"]:
                    raise StorageError("embedding_conflict", "Stored chunk embeddings are immutable.")

    def activate_version(self, version_id, space_id, lease: dict):
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            version = self._owned_version(connection, version_id, lease)
            corpus = _required_row(connection.execute("SELECT * FROM evidence_corpora WHERE id=%s FOR UPDATE",
                                        (version["corpus_id"],)).fetchone())
            if corpus["space_id"] != space_id or version["space_id"] != space_id:
                raise StorageError("space_changed", "Corpus embedding space changed before activation.")
            if version["active_version_id"] == version_id:
                return {"version_id": version_id, "corpus_revision": corpus["revision"], "state": "ready"}
            if version["version_no"] != version["latest_version_no"]:
                raise StorageError("superseded", "A newer source version was requested; this version cannot activate.")
            if version["state"] != "extracted":
                raise StorageError("invalid_state", "Source extraction is incomplete or requires review.")
            counts = _required_row(connection.execute(
                "SELECT count(*) AS chunks,count(e.chunk_id) AS vectors FROM evidence_chunks ch "
                "LEFT JOIN evidence_chunk_embeddings e ON e.chunk_id=ch.id AND e.space_id=%s "
                "WHERE ch.version_id=%s", (space_id, version_id),
            ).fetchone())
            if not counts["chunks"] or counts["chunks"] != counts["vectors"]:
                raise StorageError("incomplete_embeddings", "Every source chunk requires a valid embedding.")
            active_count = _required_row(connection.execute(
                "SELECT count(*) AS n FROM evidence_chunks ch JOIN evidence_documents d ON d.active_version_id=ch.version_id "
                "WHERE d.corpus_id=%s AND d.deleted_at IS NULL AND d.id<>%s",
                (version["corpus_id"], version["document_id"]),
            ).fetchone())["n"]
            if active_count + counts["chunks"] > self.limits["max_active_chunks"]:
                raise StorageError("quota_exceeded", "Activating this version would exceed the corpus chunk limit.")
            self._check_quota(connection)
            connection.execute(
                "UPDATE evidence_document_versions SET state='ready',activated_at=clock_timestamp(),"
                "updated_at=clock_timestamp() WHERE id=%s", (version_id,),
            )
            connection.execute(
                "UPDATE evidence_documents SET active_version_id=%s,active_version_no=%s,"
                "updated_at=clock_timestamp() WHERE id=%s",
                (version_id, version["version_no"], version["document_id"]),
            )
            revision = _required_row(connection.execute(
                "UPDATE evidence_corpora SET revision=revision+1 WHERE id=%s RETURNING revision",
                (version["corpus_id"],),
            ).fetchone())["revision"]
            return {"version_id": version_id, "corpus_revision": revision, "state": "ready"}

    def retrieve(self, corpus_id, space_id, query, vector, dense_limit=40, lexical_limit=40) -> dict:
        if not isinstance(query, str):
            raise StorageError("invalid_data", "Query must be text.")
        if any(isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1000
               for limit in (dense_limit, lexical_limit)):
            raise StorageError("invalid_data", "Retrieval limits must be between zero and 1000.")
        with self._transaction(readonly=True) as connection:
            corpus = connection.execute(
                "SELECT c.*,s.dimensions FROM evidence_corpora c JOIN evidence_embedding_spaces s ON s.id=c.space_id "
                "WHERE c.id=%s", (corpus_id,),
            ).fetchone()
            if not corpus:
                raise StorageError("not_found", "Corpus not found.")
            if corpus["space_id"] != space_id:
                raise StorageError("space_changed", "Corpus embedding space changed during query embedding.")
            literal = _vector_literal(_vector(vector, corpus["dimensions"]))
            fields = (
                'ch.id,ch.version_id,d.id AS document_id,v.name AS title,ch.text,ch.text_hash,ch.page,'
                'ch.start_offset AS "start",ch.end_offset AS "end"'
            )
            joins = (
                " FROM evidence_chunks ch JOIN evidence_documents d ON d.active_version_id=ch.version_id "
                "JOIN evidence_document_versions v ON v.id=ch.version_id "
                "JOIN evidence_chunk_embeddings e ON e.chunk_id=ch.id AND e.space_id=%s "
            )
            filters = "d.corpus_id=%s AND d.deleted_at IS NULL AND v.state='ready' AND v.space_id=%s"
            dense = connection.execute(
                "SELECT " + fields + ",1-(e.embedding <=> %s::vector) AS score" + joins +
                " WHERE " + filters + " ORDER BY e.embedding <=> %s::vector,ch.id LIMIT %s",
                (literal, space_id, corpus_id, space_id, literal, dense_limit),
            ).fetchall() if dense_limit else []
            lexical = connection.execute(
                "WITH q AS (SELECT websearch_to_tsquery('english'::regconfig,%s) AS query) "
                "SELECT " + fields + ",ts_rank_cd(ch.lexical,q.query) AS score" + joins +
                "CROSS JOIN q WHERE " + filters +
                " AND ch.lexical@@q.query ORDER BY score DESC,ch.id LIMIT %s",
                (query, space_id, corpus_id, space_id, lexical_limit),
            ).fetchall() if lexical_limit else []
            for rows in (dense, lexical):
                for rank, row in enumerate(rows, 1):
                    row["rank"] = rank
            return _json_safe({"corpus_revision": corpus["revision"], "space_id": space_id,
                               "dense": dense, "lexical": lexical})

    def enqueue_job(self, kind, payload) -> dict:
        if not isinstance(kind, str) or not kind or not isinstance(payload, dict):
            raise StorageError("invalid_data", "A job kind and JSON payload are required.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            self._check_quota(connection)
            return _json_safe(self._insert_job(connection, kind, payload))

    def claim_job(self, worker_id, lease_seconds=120) -> dict | None:
        if not isinstance(worker_id, str) or not worker_id or not 0 < lease_seconds <= 86400:
            raise StorageError("invalid_data", "A worker ID and positive bounded lease are required.")
        with self._transaction() as connection:
            job = connection.execute(
                "SELECT id FROM evidence_jobs WHERE status='queued' OR "
                "(status='running' AND lease_until<=clock_timestamp()) "
                "ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not job:
                return None
            row = connection.execute(
                "UPDATE evidence_jobs SET status='running',worker_id=%s,token=%s,"
                "lease_until=clock_timestamp()+make_interval(secs=>%s),attempts=attempts+1,"
                "updated_at=clock_timestamp() WHERE id=%s RETURNING *",
                (worker_id, _id(), float(lease_seconds), job["id"]),
            ).fetchone()
            return _json_safe(row)

    def renew_job(self, job_id, token, lease_seconds=120) -> bool:
        if not 0 < lease_seconds <= 86400:
            raise StorageError("invalid_data", "Lease duration is outside the allowed range.")
        with self._transaction() as connection:
            row = connection.execute(
                "UPDATE evidence_jobs SET lease_until=clock_timestamp()+make_interval(secs=>%s),"
                "updated_at=clock_timestamp() WHERE id=%s AND token=%s AND status='running' "
                "AND lease_until>clock_timestamp() RETURNING id", (float(lease_seconds), job_id, token),
            ).fetchone()
            return row is not None

    def finish_job(self, job_id, token, status, result=None, error=None):
        if status in {"completed", "done"}:
            status = "succeeded"
        if status not in {"succeeded", "failed", "cancelled", "needs_review", "needs_ocr"}:
            raise StorageError("invalid_data", "A terminal job status is required.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            job = self._assert_lease(connection, {"id": job_id, "token": token})
            if result is not None:
                encoded = _json(result, redact=True)
                additional = len(json.dumps(encoded.obj, ensure_ascii=False).encode("utf-8"))
                self._check_quota(connection, additional)
            connection.execute(
                "UPDATE evidence_jobs SET status=%s,result=%s,error=%s,token=NULL,lease_until=NULL,"
                "finished_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
                (status, _json(result, redact=True), _json(error, redact=True), job_id),
            )
            if job["kind"] == "ingest" and status == "failed":
                connection.execute(
                    "UPDATE evidence_document_versions SET state=CASE WHEN state='queued' THEN 'failed' ELSE state END,"
                    "updated_at=clock_timestamp() WHERE id=%s", (job["payload"].get("version_id"),),
                )
            return {"id": job_id, "status": status}

    def get_job(self, job_id) -> dict:
        with self._transaction(readonly=True) as connection:
            row = connection.execute("SELECT * FROM evidence_jobs WHERE id=%s", (job_id,)).fetchone()
            if not row:
                raise StorageError("not_found", "Job not found.")
            return _json_safe(row)

    def list_jobs(self, kind=None, limit=100) -> list[dict]:
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_jobs WHERE (%s::text IS NULL OR kind=%s) ORDER BY created_at DESC,id LIMIT %s",
                (kind, kind, max(1, min(int(limit), 1000))),
            ).fetchall()
            return _json_safe(rows)

    def cancel_job(self, job_id):
        with self._transaction() as connection:
            # Result writes and source purge take this lock before job/run rows.
            # Keep cancellation in the same order to avoid an inverted row-lock cycle.
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            job = connection.execute("SELECT * FROM evidence_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if not job:
                raise StorageError("not_found", "Job not found.")
            if job["status"] in {"queued", "running"}:
                connection.execute(
                    "UPDATE evidence_jobs SET status='cancelled',token=NULL,lease_until=NULL,"
                    "finished_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s", (job_id,),
                )
                if job["payload"].get("run_id"):
                    connection.execute(
                        "UPDATE evidence_runs SET status='cancelled',finished_at=clock_timestamp(),"
                        "updated_at=clock_timestamp() WHERE id=%s AND status<>ALL(%s)",
                        (job["payload"]["run_id"], list(TERMINAL_RUNS)),
                    )
            return {"id": job_id, "status": "cancelled" if job["status"] in {"queued", "running"}
                    else job["status"]}

    def retry_job(self, job_id) -> dict:
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            row = connection.execute(
                "UPDATE evidence_jobs SET status='queued',token=NULL,lease_until=NULL,worker_id=NULL,"
                "finished_at=NULL,error=NULL,updated_at=clock_timestamp() WHERE id=%s "
                "AND status IN ('failed','cancelled','needs_review','needs_ocr') RETURNING *", (job_id,),
            ).fetchone()
            if not row:
                raise StorageError("invalid_state", "Only stopped ingestion or evaluation jobs can be retried.")
            if row["kind"] == "query":
                raise StorageError("invalid_state", "Create a new query run to retry a terminal query.")
            return _json_safe(row)

    def create_run(self, question, corpus_id="default", settings=None) -> dict:
        if not isinstance(question, str) or not question.strip():
            raise StorageError("invalid_data", "A nonempty question is required.")
        if len(question.encode("utf-8")) > 65536:
            raise StorageError("invalid_data", "Question exceeds the application input limit.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            self._check_quota(connection, len(question.encode("utf-8")))
            corpus = connection.execute("SELECT * FROM evidence_corpora WHERE id=%s", (corpus_id,)).fetchone()
            if not corpus:
                raise StorageError("not_found", "Corpus not found.")
            accepted_settings = dict(settings or {})
            conversation_id = accepted_settings.pop("conversation_id", None)
            if conversation_id:
                try:
                    conversation_id = str(uuid.UUID(str(conversation_id)))
                except ValueError:
                    raise StorageError("invalid_data", "Conversation ID must be a UUID.") from None
                conversation = connection.execute(
                    "SELECT id FROM evidence_conversations WHERE id=%s AND corpus_id=%s FOR UPDATE",
                    (conversation_id, corpus_id),
                ).fetchone()
                if not conversation:
                    raise StorageError("not_found", "Conversation not found in this corpus.")
            else:
                conversation_id = _id()
                connection.execute(
                    "INSERT INTO evidence_conversations(id,corpus_id) VALUES (%s,%s)",
                    (conversation_id, corpus_id),
                )
            accepted_settings["memory"] = []
            if self.memory["enabled"]:
                turns = connection.execute(
                    "SELECT id,question,data->>'answer' AS answer FROM evidence_runs "
                    "WHERE conversation_id=%s AND status='answered' "
                    "ORDER BY finished_at DESC,id DESC LIMIT %s",
                    (conversation_id, self.memory["max_turns"]),
                ).fetchall()
                accepted_settings["memory"] = bounded_turns(
                    turns, self.memory["max_turns"], self.memory["max_context_bytes"],
                )
            serialized_settings = _json(accepted_settings, redact=True)
            self._check_quota(connection, len(json.dumps(serialized_settings.obj).encode("utf-8")))
            run_id = _id()
            job = self._insert_job(connection, "query", {"run_id": run_id, "corpus_id": corpus_id})
            row = _required_row(connection.execute(
                "INSERT INTO evidence_runs(id,job_id,corpus_id,space_id,question,settings,conversation_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (run_id, job["id"], corpus_id, corpus["space_id"], question,
                 serialized_settings, conversation_id),
            ).fetchone())
            return self._run_dict(row)

    def _run_dict(self, row):
        data = dict(row.pop("data", {}) or {})
        return _json_safe({**data, **row, "answer": data.get("answer")})

    def update_run(self, run_id, fields: dict, lease: dict | None = None):
        if not isinstance(fields, dict):
            raise StorageError("invalid_data", "Run fields must be a JSON object.")
        forbidden = {"id", "job_id", "corpus_id", "space_id", "question", "created_at", "settings", "data", "conversation_id"}
        if forbidden.intersection(fields):
            raise StorageError("invalid_data", "Immutable run fields cannot be changed.")
        payload = {key: value for key, value in fields.items() if key != "status"}
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            self._assert_lease(connection, lease, run_id=run_id)
            row = connection.execute("SELECT * FROM evidence_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if not row:
                raise StorageError("not_found", "Query run not found.")
            if row["status"] in TERMINAL_RUNS:
                raise StorageError("run_terminal", "A terminal query run cannot be republished.")
            status = fields.get("status", row["status"])
            valid = TERMINAL_RUNS | {"queued", "retrieving", "generating", "verifying", "repairing"}
            if status not in valid:
                raise StorageError("invalid_data", "Unknown query status.")
            if payload.get("answer") and status != "answered":
                raise StorageError("invalid_state", "Only an answered run may publish an answer.")
            if status not in {"failed", "cancelled", "timed_out", "verification_unavailable"}:
                old_size = len(json.dumps(row["data"], ensure_ascii=False).encode("utf-8"))
                new_size = len(json.dumps({**row["data"], **_metadata(payload)}, ensure_ascii=False).encode("utf-8"))
                self._check_quota(connection, max(0, new_size - old_size))
            row = connection.execute(
                "UPDATE evidence_runs SET status=%s,data=data||%s,updated_at=clock_timestamp(),"
                "finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END WHERE id=%s RETURNING *",
                (status, _json(payload, redact=True), status in TERMINAL_RUNS, run_id),
            ).fetchone()
            return self._run_dict(row)

    def get_run(self, run_id) -> dict:
        with self._transaction(readonly=True) as connection:
            row = connection.execute("SELECT * FROM evidence_runs WHERE id=%s", (run_id,)).fetchone()
            if not row:
                raise StorageError("not_found", "Query run not found.")
            events = connection.execute(
                "SELECT id,event,created_at FROM evidence_run_events WHERE run_id=%s ORDER BY id", (run_id,),
            ).fetchall()
            result = self._run_dict(row)
            result["events"] = _json_safe(events)
            return result

    def list_runs(self, limit=30, corpus_id=None) -> list[dict]:
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_runs WHERE (%s::text IS NULL OR corpus_id=%s) "
                "ORDER BY created_at DESC,id LIMIT %s",
                (corpus_id, corpus_id, max(1, min(int(limit), 1000))),
            ).fetchall()
            return [self._run_dict(row) for row in rows]

    def append_event(self, run_id, event: dict, lease=None):
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            self._assert_lease(connection, lease, run_id=run_id)
            serialized = _json(event, redact=True)
            self._check_quota(connection, len(json.dumps(serialized.obj, ensure_ascii=False).encode("utf-8")))
            row = connection.execute(
                "INSERT INTO evidence_run_events(run_id,event) VALUES (%s,%s) RETURNING id,created_at",
                (run_id, _json(event, redact=True)),
            ).fetchone()
            return _json_safe(row)

    def reserve_call(self, run_id, phase, profile, estimated_cost, limits: dict) -> str:
        if any(not isinstance(value, str) or not value or len(value) > 2048
               for value in (run_id, phase, profile)):
            raise StorageError("invalid_data", "Call identity, phase, and profile are required.")
        mock = bool(limits.get("mock", False))
        estimate = Decimal(0) if mock else _cost(estimated_cost)
        total_cap = _cost(limits.get("total_cap", 0))
        phase_caps = limits.get("phase_caps") or {}
        phase_cap = _cost(phase_caps.get(phase, 0))
        try:
            attempt_cap = int(limits.get("run_attempt_cap", 10))
            concurrency = int(limits.get("remote_concurrency", 4))
            profile_concurrency = int(limits.get("profile_concurrency", concurrency))
            timeout = float(limits.get("timeout_seconds", 30))
            if (attempt_cap < 1 or concurrency < 1 or profile_concurrency < 1
                    or not math.isfinite(timeout) or not 0 < timeout <= 86400):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise StorageError("invalid_data", "Call attempt, concurrency, and timeout limits must be positive.") from None
        if not mock and (total_cap <= 0 or phase_cap <= 0):
            raise StorageError("budget_exhausted", "Live calls require positive total and phase budgets.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_BUDGET_LOCK,))
            connection.execute(
                "UPDATE evidence_calls SET status='expired_unknown',finished_at=clock_timestamp(),"
                "detail=COALESCE(detail,'{}'::jsonb)||'{\"reason\":\"reservation_expired_usage_unknown\"}'::jsonb "
                "WHERE status='reserved' AND active_until<=clock_timestamp()"
            )
            run = connection.execute("SELECT status FROM evidence_runs WHERE id=%s", (run_id,)).fetchone()
            if run and run["status"] in TERMINAL_RUNS:
                raise StorageError("cancelled", "This query run is already terminal.")
            # A purged query no longer has a evidence_runs row, but its stopped durable
            # job remains. It must still prevent a stale worker from booking a call.
            direct_jobs = connection.execute(
                "SELECT status FROM evidence_jobs WHERE id=%s OR "
                "(kind='query' AND payload->>'run_id'=%s)", (run_id, run_id),
            ).fetchall()
            if any(job["status"] not in {"queued", "running"} for job in direct_jobs):
                raise StorageError("cancelled", "The owning job has stopped.")
            # Evaluation/ingestion contexts are not query rows; check their owning job when present.
            owner_job = None
            if run_id.startswith("eval:"):
                owner_job = run_id.split(":", 2)[1]
            elif run_id.startswith("ingest:"):
                source_job = connection.execute(
                    "SELECT id FROM evidence_jobs WHERE kind='ingest' AND payload->>'version_id'=%s",
                    (run_id.split(":", 1)[1],),
                ).fetchone()
                owner_job = source_job["id"] if source_job else None
            if owner_job:
                owner = connection.execute("SELECT status FROM evidence_jobs WHERE id=%s", (owner_job,)).fetchone()
                if owner and owner["status"] not in {"queued", "running"}:
                    raise StorageError("cancelled", "The owning job has stopped.")
            stats = _required_row(connection.execute(
                "SELECT COALESCE(sum(charged_cost),0) AS total,"
                "COALESCE(sum(charged_cost) FILTER (WHERE phase=%s),0) AS phase,"
                "count(*) FILTER (WHERE run_id=%s) AS attempts,"
                "count(*) FILTER (WHERE status='reserved' AND active_until>clock_timestamp()) AS active,"
                "count(*) FILTER (WHERE status='reserved' AND active_until>clock_timestamp() AND profile=%s) AS active_profile "
                "FROM evidence_calls WHERE mock=%s", (phase, run_id, profile, mock),
            ).fetchone())
            if stats["attempts"] >= attempt_cap:
                raise StorageError("budget_exhausted", "Persistent run attempt limit reached.")
            if stats["active"] >= concurrency:
                raise StorageError("concurrency_limited", "Outbound concurrency limit is currently full.",
                                   retryable=True)
            if stats["active_profile"] >= profile_concurrency:
                raise StorageError("concurrency_limited", "Provider profile concurrency limit is currently full.",
                                   retryable=True)
            if not mock and (stats["total"] + estimate > total_cap or stats["phase"] + estimate > phase_cap):
                raise StorageError("budget_exhausted", "Insufficient remaining total or phase budget.")
            call_id = _id()
            connection.execute(
                "INSERT INTO evidence_calls(id,run_id,phase,profile,mock,estimated_cost,charged_cost,active_until) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+make_interval(secs=>%s))",
                (call_id, run_id, phase, profile, mock, estimate, estimate, timeout),
            )
            return call_id

    def finish_call(self, call_id, status, usage=None, actual_cost=None, detail=None):
        if not isinstance(status, str) or not status or status == "reserved":
            raise StorageError("invalid_data", "A completed call status is required.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_BUDGET_LOCK,))
            row = connection.execute("SELECT * FROM evidence_calls WHERE id=%s FOR UPDATE", (call_id,)).fetchone()
            if not row:
                raise StorageError("not_found", "Call reservation not found.")
            actual = Decimal(0) if row["mock"] else (_cost(actual_cost) if actual_cost is not None else None)
            # Final known accounting is immutable; repeated completion cannot lower recorded spend.
            if row["cost_known"]:
                if actual is not None and actual != row["actual_cost"]:
                    raise StorageError("invalid_data", "A known call charge cannot be rewritten.")
                return _json_safe(row)
            charged = actual if actual is not None else row["charged_cost"]
            metadata = _metadata(detail) if isinstance(detail, dict) else {"detail": detail}
            if actual is not None and actual > row["estimated_cost"]:
                metadata["estimate_exceeded"] = True
            row = connection.execute(
                "UPDATE evidence_calls SET status=%s,usage=%s,actual_cost=%s,charged_cost=%s,"
                "cost_known=%s,detail=%s,finished_at=clock_timestamp() WHERE id=%s RETURNING *",
                (status, _json(usage, redact=True), actual, charged, actual is not None,
                 _json(metadata, redact=True), call_id),
            ).fetchone()
            return _json_safe(row)

    def get_calls(self, run_id=None, prefix=None) -> list[dict]:
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_calls WHERE (%s::text IS NULL OR run_id=%s) "
                "AND (%s::text IS NULL OR left(run_id,length(%s))=%s) ORDER BY created_at,id",
                (run_id, run_id, prefix, prefix, prefix),
            ).fetchall()
            return _json_safe(rows)

    def budget_summary(self) -> dict:
        with self._transaction(readonly=True) as connection:
            rows = connection.execute(
                "SELECT mock,phase,count(*) AS attempts,COALESCE(sum(estimated_cost),0) AS estimated_cost,"
                "COALESCE(sum(charged_cost),0) AS charged_cost,"
                "COALESCE(sum(actual_cost) FILTER (WHERE cost_known),0) AS known_actual_cost,"
                "count(*) FILTER (WHERE NOT cost_known) AS unknown_cost_calls,"
                "count(*) FILTER (WHERE status='reserved' AND active_until>clock_timestamp()) AS active_calls "
                "FROM evidence_calls GROUP BY mock,phase ORDER BY mock,phase"
            ).fetchall()
            actual_disk = _required_row(connection.execute(
                "SELECT pg_database_size(current_database()) AS database_bytes"
            ).fetchone())["database_bytes"]
            payload = self._payload_bytes(connection)
            return _json_safe({"phases": rows, "retained_payload_bytes": payload,
                               "max_retained_payload_bytes": self.limits["max_retained_payload_bytes"],
                               "database_bytes": actual_disk,
                               "live_charged_cost": sum(row["charged_cost"] for row in rows if not row["mock"]),
                               "active_calls": sum(row["active_calls"] for row in rows)})

    def delete_document(self, document_id) -> dict:
        """Explicit purge includes snapshots; financial call metadata is retained."""
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            document = connection.execute(
                "SELECT * FROM evidence_documents WHERE id=%s FOR UPDATE", (document_id,),
            ).fetchone()
            if not document:
                raise StorageError("not_found", "Document not found.")
            versions = connection.execute(
                "SELECT id,job_id FROM evidence_document_versions WHERE document_id=%s", (document_id,),
            ).fetchall()
            references = [document_id] + [row["id"] for row in versions]
            # Cancel every in-flight corpus query: it may already hold the source in memory
            # even when its evidence has not yet been persisted. Lease fencing rejects later writes.
            running = connection.execute(
                "SELECT id,job_id FROM evidence_runs WHERE corpus_id=%s AND status<>ALL(%s) FOR UPDATE",
                (document["corpus_id"], list(TERMINAL_RUNS)),
            ).fetchall()
            running_jobs = [row["job_id"] for row in running]
            source_jobs = [row["job_id"] for row in versions if row["job_id"]]
            affected_runs = connection.execute(
                "SELECT r.id,r.job_id FROM evidence_runs r WHERE r.id=ANY(%s) OR "
                "EXISTS(SELECT 1 FROM unnest(%s::text[]) ref WHERE strpos(r.data::text,ref)>0) OR "
                "EXISTS(SELECT 1 FROM evidence_run_events ev,unnest(%s::text[]) ref "
                "WHERE ev.run_id=r.id AND strpos(ev.event::text,ref)>0)",
                ([row["id"] for row in running], references, references),
            ).fetchall()
            affected_jobs = connection.execute(
                "SELECT id FROM evidence_jobs j WHERE "
                "EXISTS(SELECT 1 FROM unnest(%s::text[]) ref "
                "WHERE strpos(COALESCE(j.result::text,''),ref)>0 OR strpos(j.payload::text,ref)>0) OR "
                "(kind='evaluation' AND status IN ('queued','running'))", (references,),
            ).fetchall()
            job_ids = list(set(running_jobs + source_jobs + [r["job_id"] for r in affected_runs]
                               + [r["id"] for r in affected_jobs]))
            if job_ids:
                connection.execute(
                    "UPDATE evidence_jobs SET status='cancelled',token=NULL,lease_until=NULL,result=NULL,error=NULL,"
                    "finished_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=ANY(%s)", (job_ids,),
                )
            run_ids = [row["id"] for row in affected_runs]
            if run_ids:
                connection.execute("DELETE FROM evidence_runs WHERE id=ANY(%s)", (run_ids,))
            # Source deletion also removes dialogue copied into other run snapshots.
            connection.execute(
                "UPDATE evidence_runs SET settings=settings-'memory' WHERE corpus_id=%s",
                (document["corpus_id"],),
            )
            connection.execute("UPDATE evidence_documents SET active_version_id=NULL WHERE id=%s", (document_id,))
            connection.execute("DELETE FROM evidence_documents WHERE id=%s", (document_id,))
            connection.execute(
                "DELETE FROM evidence_embedding_cache cache WHERE NOT EXISTS("
                "SELECT 1 FROM evidence_chunks ch JOIN evidence_chunk_embeddings e ON e.chunk_id=ch.id "
                "WHERE ch.text_hash=cache.text_hash AND e.space_id=cache.space_id)"
            )
            connection.execute("UPDATE evidence_corpora SET revision=revision+1 WHERE id=%s",
                               (document["corpus_id"],))
            return {"document_id": document_id, "deleted": True, "purged_runs": len(run_ids),
                    "cancelled_jobs": len(job_ids)}

    def cleanup(self, inactive_days=None, failed_days=None) -> dict:
        inactive_days = self.limits["inactive_retention_days"] if inactive_days is None else inactive_days
        failed_days = self.limits["failed_staging_retention_days"] if failed_days is None else failed_days
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (inactive_days, failed_days)):
            raise StorageError("invalid_data", "Retention days must be nonnegative integers.")
        with self._transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_QUOTA_LOCK,))
            deleted_runs = connection.execute(
                "DELETE FROM evidence_runs WHERE status=ANY(%s) AND finished_at < "
                "clock_timestamp()-make_interval(days=>%s) RETURNING id",
                (list(TERMINAL_RUNS), inactive_days),
            ).fetchall()
            deleted_versions = connection.execute(
                "DELETE FROM evidence_document_versions v USING evidence_documents d "
                "WHERE d.id=v.document_id AND d.active_version_id IS DISTINCT FROM v.id AND "
                "NOT EXISTS(SELECT 1 FROM evidence_jobs j WHERE j.id=v.job_id AND j.status IN ('queued','running')) AND "
                "v.updated_at < clock_timestamp()-make_interval(days=>"
                "CASE WHEN v.state IN ('queued','failed','needs_review','needs_ocr','extracted') THEN %s ELSE %s END) "
                "AND NOT EXISTS(SELECT 1 FROM evidence_runs r WHERE strpos(r.data::text,v.id)>0) "
                "AND NOT EXISTS(SELECT 1 FROM evidence_run_events e WHERE strpos(e.event::text,v.id)>0) "
                "AND NOT EXISTS(SELECT 1 FROM evidence_jobs j WHERE strpos(COALESCE(j.result::text,''),v.id)>0 "
                "AND j.kind='evaluation') RETURNING v.id", (failed_days, inactive_days),
            ).fetchall()
            connection.execute(
                "DELETE FROM evidence_embedding_cache cache WHERE NOT EXISTS("
                "SELECT 1 FROM evidence_chunks ch JOIN evidence_chunk_embeddings e ON e.chunk_id=ch.id "
                "WHERE ch.text_hash=cache.text_hash AND e.space_id=cache.space_id)"
            )
            connection.execute(
                "DELETE FROM evidence_documents d WHERE d.active_version_id IS NULL AND NOT EXISTS("
                "SELECT 1 FROM evidence_document_versions v WHERE v.document_id=d.id)"
            )
            connection.execute(
                "DELETE FROM evidence_conversations c WHERE NOT EXISTS("
                "SELECT 1 FROM evidence_runs r WHERE r.conversation_id=c.id)"
            )
            return {"deleted_runs": len(deleted_runs), "deleted_versions": len(deleted_versions),
                    "retained_payload_bytes": int(self._payload_bytes(connection))}
