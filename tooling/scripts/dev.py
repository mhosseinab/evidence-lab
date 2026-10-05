"""Run the local API, worker and Vite together with scoped process cleanup."""

import argparse
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time

import psycopg

from evidence_lab.config import ConfigError, load_config
from evidence_lab.storage import Store

ROOT = Path(__file__).resolve().parents[2]


def database_available(config) -> bool:
    try:
        with psycopg.connect(config.database.dsn, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


def prepare_database(config, config_path: str) -> bool:
    default_mock = (
        Path(config_path).resolve() == ROOT / "configs/mock.yaml"
        and config.runtime.mode == "mock"
        and config.database.dsn == "postgresql://rag:rag@localhost:5432/rag"
    )
    migration_command = f"task app:migrate CONFIG={shlex.quote(config_path)}"
    if default_mock:
        try:
            if not database_available(config):
                print("Starting the local development PostgreSQL database...", flush=True)
                subprocess.run(
                    ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "60", "db"],
                    cwd=ROOT,
                    check=True,
                )
            subprocess.run(
                [sys.executable, "-m", "evidence_lab", "migrate", "--config", config_path],
                cwd=ROOT,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            print(
                f"Could not prepare the development database. Check Docker/PostgreSQL, then run {migration_command}.",
                file=sys.stderr,
            )
            return False
    elif not database_available(config):
        print(
            f"The configured development database is unavailable. Start its PostgreSQL service, then run {migration_command}.",
            file=sys.stderr,
        )
        return False
    if not Store(config.database.dsn).health():
        print(
            f"The development database needs PostgreSQL/pgvector migrations. Run {migration_command} before task dev.",
            file=sys.stderr,
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/mock.yaml")
    parser.add_argument("--dashboard-port", type=int, default=5173)
    args = parser.parse_args()
    if not 1 <= args.dashboard_port <= 65535:
        parser.error("Dashboard port must be between 1 and 65535.")
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    if not prepare_database(config, args.config):
        return 1

    host = config.runtime.host
    proxy_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in proxy_host:
        proxy_host = f"[{proxy_host}]"
    environment = {**os.environ, "EVIDENCE_LAB_API_URL": f"http://{proxy_host}:{config.runtime.port}"}
    commands = {
        "API": [sys.executable, "-m", "evidence_lab", "serve", "--config", args.config],
        "worker": [sys.executable, "-m", "evidence_lab", "worker", "--config", args.config],
        "dashboard": [
            "pnpm",
            "--filter",
            "@evidence-lab/dashboard",
            "run",
            "dev",
            "--port",
            str(args.dashboard_port),
            "--strictPort",
        ],
    }
    processes: dict[str, subprocess.Popen] = {}
    interrupted = False

    def stop(_signal, _frame):
        nonlocal interrupted
        interrupted = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        for name, command in commands.items():
            if interrupted:
                return 130
            processes[name] = subprocess.Popen(command, cwd=ROOT, env=environment, start_new_session=True)
        while not interrupted:
            for name, process in processes.items():
                status = process.poll()
                if status is not None:
                    print(
                        f"Development {name} exited ({status}); stopping the other services.", file=sys.stderr
                    )
                    return status if status > 0 else 1
            time.sleep(0.1)
        return 130
    except OSError:
        print(
            "Could not start development services. Run task setup and check that pnpm is installed.",
            file=sys.stderr,
        )
        return 1
    finally:
        for process in processes.values():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        for process in processes.values():
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
