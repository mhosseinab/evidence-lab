#!/usr/bin/env python3
"""Restore a backup into an empty native PostgreSQL database.

The pgvector extension must be installed on the target server. Existing user
tables/views/sequences cause refusal; this helper never uses --clean or drops data.
No remote inference or automatic re-embedding is performed.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

import psycopg

from evidence_lab.config import load_config
from backup import connection_env, require_tool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Runtime YAML pointing to the empty target database")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise SystemExit("Backup input file does not exist.")
    config = load_config(args.config)
    try:
        with psycopg.connect(config.database.dsn, connect_timeout=10) as connection:
            present = connection.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','m','v','S')"
            ).fetchone()[0]
            if present:
                raise SystemExit("Restore refused: target database contains user objects. Use a new empty database.")
            available = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='vector')"
            ).fetchone()[0]
            if not available:
                raise SystemExit("Install the pgvector extension on the target PostgreSQL server first.")
        env = connection_env(config.database.dsn)
        subprocess.run(
            [require_tool("pg_restore"), "--no-password", "--exit-on-error", "--single-transaction",
             "--no-owner", "--no-acl", "--dbname", env.get("PGDATABASE", ""), str(source)],
            env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=args.timeout,
        )
        print("Restore completed. Validate schema revision and source retrieval before starting workers.")
    except (psycopg.Error, subprocess.SubprocessError, OSError):
        raise SystemExit("Restore failed. Check the empty target, extension availability, permissions, and client version.") from None


if __name__ == "__main__":
    main()
