"""Opt-in native pg_dump/pg_restore gate using dedicated disposable databases.

EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN explicitly authorizes creation/deletion of uniquely
named test databases. EVIDENCE_LAB_TEST_DSN alone is never sufficient. No existing
database is migrated, seeded, restored, truncated or dropped by this test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import psycopg
import pytest
import yaml
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from evidence_lab.config import load_config
from evidence_lab.domain import stable_hash
from evidence_lab.ingestion import extract_document, pipeline_revision
from evidence_lab.retrieval import space_manifest
from evidence_lab.storage import Store

ROOT = Path(__file__).resolve().parents[3]


def _child_database_url(admin_dsn: str, name: str) -> str:
    """Keep explicit libpq connection settings while overriding only dbname.

    All copied settings stay in a private config file, never an argv value. The
    URI's query host overrides its placeholder authority, supporting explicit
    Unix-socket hosts as well as TCP/SSL options without shell quoting.
    """
    options = conninfo_to_dict(admin_dsn)
    if not options.get("host"):
        raise ValueError("Native admin DSN requires an explicit host or Unix-socket directory")
    copied = {key: value for key, value in options.items() if key != "dbname"}
    result = "postgresql://localhost/" + quote(name, safe="") + "?" + urlencode(copied)
    resolved = conninfo_to_dict(result)
    if resolved.get("dbname") != name or any(resolved.get(key) != value for key, value in copied.items()):
        raise ValueError("Dedicated database connection could not be constructed safely")
    return result


@pytest.fixture
def native_restore_databases():
    if os.environ.get("EVIDENCE_LAB_TEST_BACKEND", "").lower() == "pglite":
        pytest.skip("Native restore gate cannot run on PGlite")
    admin_dsn = os.environ.get("EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("Set EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN to explicitly authorize isolated native test databases")
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        pytest.skip("Native restore gate requires both pg_dump and pg_restore clients")

    nonce = uuid.uuid4().hex
    source_name = f"evidence_restore_it_{nonce}_src"
    target_name = f"evidence_restore_it_{nonce}_dst"
    try:
        source_url = _child_database_url(admin_dsn, source_name)
        target_url = _child_database_url(admin_dsn, target_name)
    except (psycopg.Error, ValueError):
        pytest.skip("Native admin DSN cannot safely address two dedicated test databases")

    admin = None
    created: dict[str, tuple[int, int] | None] = {}
    cleanup_failures: list[str] = []
    try:
        try:
            admin = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10)
            version = admin.execute("SELECT version()").fetchone()[0]
            if "pglite" in version.lower():
                pytest.skip("Native pg_dump/restore verification requires native PostgreSQL")
            capable = admin.execute("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname=current_user").fetchone()[0]
            available = admin.execute("SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='vector')").fetchone()[0]
            if not capable:
                pytest.skip("Native admin role cannot create dedicated temporary databases")
            if not available:
                pytest.skip("Native PostgreSQL server does not have pgvector installed")
            admin.execute("SET statement_timeout='20s'")
            admin.execute("SET lock_timeout='5s'")
            for name in (source_name, target_name):
                # No IF NOT EXISTS: a collision must never authorize operations
                # on a database that this invocation did not create.
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
                created[name] = None
                row = admin.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (name,)).fetchone()
                if not row:
                    raise ValueError("Created database identity could not be verified")
                created[name] = (row[0], row[1])
        except (psycopg.Error, ValueError):
            pytest.skip("Could not safely create dedicated native restore test databases")

        try:
            # Verify libpq actually selected our unique new databases before any
            # migration, and confirm extension installation privileges up front.
            for url, name in ((source_url, source_name), (target_url, target_name)):
                with psycopg.connect(url, connect_timeout=10) as connection:
                    actual = connection.execute("SELECT current_database()").fetchone()[0]
                    if actual != name:
                        raise ValueError("Dedicated database identity mismatch")
            with psycopg.connect(source_url, connect_timeout=10) as connection:
                connection.execute("CREATE EXTENSION vector")
        except (psycopg.Error, ValueError):
            pytest.skip("Dedicated database connection or pgvector installation privilege is unavailable")

        yield {"source_url": source_url, "target_url": target_url}
    finally:
        # Recheck OID and owner before dropping. A replaced database with the
        # same name is left untouched. Never terminate unrelated backends and
        # never use a wildcard, DROP ... CASCADE or existing-database --clean.
        if admin is not None and not admin.closed:
            for name, expected in reversed(list(created.items())):
                try:
                    current = admin.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (name,)).fetchone()
                    if current is None:
                        continue
                    if expected is None or tuple(current) != expected:
                        cleanup_failures.append(name)
                        continue
                    admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
                except psycopg.Error:
                    cleanup_failures.append(name)
            admin.close()
        elif created:
            cleanup_failures.extend(created)
        if cleanup_failures:
            pytest.fail("Could not safely remove dedicated test databases: " + ", ".join(cleanup_failures), pytrace=False)


def _write_private_config(path: Path, dsn: str):
    values = yaml.safe_load((ROOT / "configs/mock.yaml").read_text())
    values["database"]["dsn"] = dsn
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        yaml.safe_dump(values, handle, sort_keys=False)


def _run_helper(script: str, config_path: Path, direction: str, archive: Path) -> subprocess.CompletedProcess:
    helper_env = os.environ.copy()
    helper_env["PYTHONPATH"] = str(ROOT / "apps/evidence-lab/src") + (os.pathsep + helper_env["PYTHONPATH"] if helper_env.get("PYTHONPATH") else "")
    # Credential values occur only in the private config; argv includes paths.
    return subprocess.run(
        [sys.executable, str(ROOT / "tooling/scripts" / script), "--config", str(config_path), direction, str(archive), "--timeout", "45"],
        cwd=ROOT, env=helper_env, capture_output=True, text=True, timeout=55,
    )


@pytest.mark.integration
@pytest.mark.native_postgres
def test_native_pg_dump_restore_preserves_sources_vectors_and_retrieval(native_restore_databases, tmp_path):
    source_url = native_restore_databases["source_url"]
    target_url = native_restore_databases["target_url"]
    source_config = tmp_path / "source.private.yaml"
    target_config = tmp_path / "target.private.yaml"
    archive = tmp_path / "native-restore.dump"
    try:
        _write_private_config(source_config, source_url)
        _write_private_config(target_config, target_url)
        cfg = load_config(str(source_config))
        source = Store(source_url, cfg.ingestion.model_dump())
        target = Store(target_url, cfg.ingestion.model_dump())
        source.migrate()
        manifest = space_manifest(cfg)
        source.ensure_corpus("default", manifest)
        raw = "Backup validation fixture. Preserve UTF-8 bytes: café, −5 mg, and 🧪.\nNo model inference is used.".encode()
        upload = source.create_document("restore-source.txt", raw, "text/plain", pipeline_revision=pipeline_revision(cfg))
        job = source.claim_job("native-restore-fixture", lease_seconds=120)
        assert job is not None and job["id"] == upload["job_id"]
        extracted = extract_document(raw, "restore-source.txt", "text/plain", cfg)
        assert extracted["state"] == "extracted"
        chunks = extracted["chunks"]
        for chunk in chunks:
            chunk["id"] = "chunk_" + stable_hash({"version_id": upload["version_id"], "chunk": chunk})[:32]
        source.save_extraction(upload["version_id"], extracted["pages"], chunks, "extracted", job)
        fixture_vector = [1.0] + [0.0] * (manifest["dimensions"] - 1)
        source.write_embeddings(upload["version_id"], manifest["id"], {chunk["id"]: fixture_vector for chunk in chunks}, job)
        source.activate_version(upload["version_id"], manifest["id"], job)
        source.finish_job(job["id"], job["token"], "succeeded", result={"fixture_only": True, "inference_performed": False})

        run = source.create_run("Backup validation trace fixture", settings={"fixture_only": True})
        query_job = source.claim_job("native-restore-fixture", lease_seconds=120)
        assert query_job is not None and query_job["id"] == run["job_id"]
        trace_hash = stable_hash({"version_id": upload["version_id"], "chunks": chunks})
        source.append_event(run["id"], {"type": "native_restore_fixture", "source_version_id": upload["version_id"], "evidence_hash": trace_hash, "fixture_only": True}, lease=query_job)
        source.update_run(run["id"], {"status": "abstained", "answer": None, "blocks": [], "message": "Storage restoration fixture; no inference was performed.", "code": "restore_fixture", "source_version_id": upload["version_id"], "evidence_hash": trace_hash}, lease=query_job)
        source.finish_job(query_job["id"], query_job["token"], "succeeded", result={"fixture_only": True})

        hashes = [chunk["text_hash"] for chunk in chunks]
        expected_version = source.get_version(upload["version_id"], include_bytes=True)
        expected_chunks = source.chunks_for_version(upload["version_id"])
        expected_vectors = source.get_cached_embeddings(manifest["id"], hashes)
        expected_retrieval = source.retrieve("default", manifest["id"], "backup validation", fixture_vector)
        expected_run = source.get_run(run["id"])
        with psycopg.connect(source_url, connect_timeout=10) as connection:
            expected_revision = connection.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
        assert expected_retrieval["dense"] and expected_retrieval["lexical"]
        assert expected_vectors == {digest: fixture_vector for digest in hashes}
        assert source.get_calls() == []

        backup = _run_helper("backup.py", source_config, "--output", archive)
        if backup.returncode != 0:
            pytest.fail(f"Native backup helper failed with exit code {backup.returncode}; diagnostics suppressed to keep connection details private", pytrace=False)
        assert archive.is_file() and archive.stat().st_size > 0
        with archive.open("rb") as handle:
            assert handle.read(5) == b"PGDMP"

        restored = _run_helper("restore.py", target_config, "--input", archive)
        if restored.returncode != 0:
            pytest.fail(f"Native restore helper failed with exit code {restored.returncode}; diagnostics suppressed to keep connection details private", pytrace=False)
        assert target.health() is True
        assert target.get_version(upload["version_id"], include_bytes=True) == expected_version
        assert target.chunks_for_version(upload["version_id"]) == expected_chunks
        assert target.get_cached_embeddings(manifest["id"], hashes) == expected_vectors
        assert target.retrieve("default", manifest["id"], "backup validation", fixture_vector) == expected_retrieval
        assert target.get_run(run["id"]) == expected_run
        with psycopg.connect(target_url, connect_timeout=10) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall() == expected_revision
        assert target.get_calls() == []

        # Exercise the shipped refusal path against a now-populated target and
        # prove it preserved the restored contents rather than overwriting them.
        refused = _run_helper("restore.py", target_config, "--input", archive)
        assert refused.returncode != 0
        assert "target database contains user objects" in refused.stderr
        assert target.get_version(upload["version_id"], include_bytes=True) == expected_version
        assert target.get_cached_embeddings(manifest["id"], hashes) == expected_vectors
    finally:
        source_config.unlink(missing_ok=True)
        target_config.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
