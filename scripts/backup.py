#!/usr/bin/env python3
"""Create a native PostgreSQL custom-format backup, including original sources.

Requires the PostgreSQL pg_dump client. Credentials are passed through libpq
environment variables, never printed or placed in command-line arguments.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from psycopg import Error as PostgresError
from psycopg.conninfo import conninfo_to_dict

from rag_poc.config import load_config


LIBPQ_ENV = {
    "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT",
    "dbname": "PGDATABASE", "user": "PGUSER", "password": "PGPASSWORD",
    "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY", "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR", "connect_timeout": "PGCONNECT_TIMEOUT",
    "options": "PGOPTIONS", "application_name": "PGAPPNAME",
    "target_session_attrs": "PGTARGETSESSIONATTRS", "channel_binding": "PGCHANNELBINDING",
    "service": "PGSERVICE", "passfile": "PGPASSFILE", "gssencmode": "PGGSSENCMODE",
}


def connection_env(dsn):
    options = conninfo_to_dict(dsn)
    if any(key not in LIBPQ_ENV for key in options):
        raise SystemExit("A configured database connection option is not supported by this backup helper.")
    # Do not inherit stale libpq connection values from another database.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    for key, value in options.items():
        env[LIBPQ_ENV[key]] = value
    return env


def require_tool(name):
    tool = shutil.which(name)
    if not tool:
        raise SystemExit(f"Install the native PostgreSQL {name} client before using this helper.")
    return tool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Private runtime YAML configuration")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    config = load_config(args.config)
    target = args.output.expanduser().resolve()
    if target.exists():
        raise SystemExit("Backup destination already exists; choose a new filename.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".rag-backup-", suffix=".partial",
                                         delete=False) as handle:
            temporary = Path(handle.name)
        subprocess.run(
            [require_tool("pg_dump"), "--no-password", "--format=custom", "--no-owner", "--no-acl",
             "--file", str(temporary)],
            env=connection_env(config.database.dsn), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=args.timeout,
        )
        # Hard-link creation is atomic and refuses a destination created concurrently.
        os.link(temporary, target)
        print(f"Backup created: {target}")
    except (PostgresError, subprocess.SubprocessError, OSError):
        raise SystemExit("Backup failed. Check database access, client/server compatibility, and disk space.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
