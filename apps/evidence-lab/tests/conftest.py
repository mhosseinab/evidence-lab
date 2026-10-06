"""Isolated PostgreSQL schemas for repeatable integration tests."""
import os
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from evidence_lab.storage import Store


@contextmanager
def _isolated_test_schema():
    base_dsn = os.environ.get("EVIDENCE_LAB_TEST_DSN")
    if not base_dsn:
        pytest.skip("Set EVIDENCE_LAB_TEST_DSN for PostgreSQL/pgvector integration tests.")
    if os.environ.get("EVIDENCE_LAB_TEST_BACKEND", "").lower() == "pglite":
        # This flag is supplied only by the test setup that creates and destroys a
        # fresh in-memory database per invocation. It is not a native fallback.
        Store(base_dsn).migrate()
        yield base_dsn
        return
    schema = "evidence_storage_test_" + uuid.uuid4().hex
    with psycopg.connect(base_dsn, autocommit=True) as connection:
        # Extensions are database-scoped; keep their types visible while tables
        # and test data remain in independent private schemas.
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_url(base_dsn).update_query_dict({"options": f"-csearch_path={schema},public"})
    scoped_dsn = scoped_url.render_as_string(hide_password=False)
    try:
        with psycopg.connect(scoped_dsn) as connection:
            schema_row = connection.execute("SELECT current_schema()").fetchone()
            assert schema_row is not None
            assert schema_row[0] == schema, "Test schema isolation must be enforced by the server."
            connection.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        Store(scoped_dsn).migrate()
        with psycopg.connect(scoped_dsn) as connection:
            assert connection.execute("SELECT table_schema FROM information_schema.tables "
                                      "WHERE table_name='evidence_jobs' AND table_schema=current_schema()").fetchone(), \
                "Test jobs must be isolated in the private schema."
        yield scoped_dsn
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))



@pytest.fixture(scope="session")
def storage_dsn():
    with _isolated_test_schema() as dsn:
        yield dsn


@pytest.fixture
def isolated_storage_dsn():
    with _isolated_test_schema() as dsn:
        yield dsn
