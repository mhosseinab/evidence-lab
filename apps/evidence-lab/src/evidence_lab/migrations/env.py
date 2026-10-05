"""Alembic environment; the application supplies a short-lived connection."""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def run(connection):
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    dsn = os.environ.get("EVIDENCE_LAB_DATABASE_DSN") or context.config.get_main_option("sqlalchemy.url")
    if not dsn:
        raise RuntimeError("Set EVIDENCE_LAB_DATABASE_DSN before running migrations.")
    url = make_url(dsn).set(drivername="postgresql+psycopg")
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(814372900)")
            run(connection)
    finally:
        engine.dispose()
