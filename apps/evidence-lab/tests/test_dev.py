"""Development startup prerequisites must succeed before any servers are launched."""

from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def dev():
    return runpy.run_path(str(ROOT / "tooling/scripts/dev.py"))


def config(dsn="postgresql://evidence:evidence@localhost:5432/evidence_lab"):
    return SimpleNamespace(database=SimpleNamespace(dsn=dsn), runtime=SimpleNamespace(mode="mock"))


def test_default_development_waits_for_database_before_migrations(dev, monkeypatch):
    prepare = dev["prepare_database"]
    monkeypatch.setitem(prepare.__globals__, "database_available", lambda _config: False)
    run = Mock()
    monkeypatch.setattr(dev["subprocess"], "run", run)
    monkeypatch.setitem(prepare.__globals__, "Store", lambda _dsn: SimpleNamespace(health=lambda: True))
    assert prepare(config(), "configs/mock.yaml")
    calls = [call.args[0] for call in run.call_args_list]
    assert calls[0] == ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "60", "db"]
    assert calls[1][-4:] == ["evidence_lab", "migrate", "--config", "configs/mock.yaml"]


def test_existing_default_database_still_gets_migrations(dev, monkeypatch):
    prepare = dev["prepare_database"]
    monkeypatch.setitem(prepare.__globals__, "database_available", lambda _config: True)
    run = Mock()
    monkeypatch.setattr(dev["subprocess"], "run", run)
    monkeypatch.setitem(prepare.__globals__, "Store", lambda _dsn: SimpleNamespace(health=lambda: True))
    assert prepare(config(), "configs/mock.yaml")
    assert run.call_count == 1
    assert "migrate" in run.call_args.args[0]


def test_custom_database_is_not_started_or_migrated(dev, monkeypatch, capsys):
    prepare = dev["prepare_database"]
    monkeypatch.setitem(prepare.__globals__, "database_available", lambda _config: False)
    run = Mock()
    monkeypatch.setattr(dev["subprocess"], "run", run)
    assert not prepare(config("postgresql://operator:SECRET@example.invalid/dev"), "private.yaml")
    run.assert_not_called()
    message = capsys.readouterr().err
    assert "task app:migrate" in message
    assert "SECRET" not in message


def test_failed_database_setup_stops_startup(dev, monkeypatch):
    prepare = dev["prepare_database"]
    monkeypatch.setitem(prepare.__globals__, "database_available", lambda _config: False)
    run = Mock(side_effect=dev["subprocess"].CalledProcessError(1, ["docker", "compose"]))
    monkeypatch.setattr(dev["subprocess"], "run", run)
    assert not prepare(config(), "configs/mock.yaml")
    assert run.call_count == 1


def test_unmigrated_custom_database_requires_explicit_migration(dev, monkeypatch, capsys):
    prepare = dev["prepare_database"]
    monkeypatch.setitem(prepare.__globals__, "database_available", lambda _config: True)
    monkeypatch.setitem(prepare.__globals__, "Store", lambda _dsn: SimpleNamespace(health=lambda: False))
    run = Mock()
    monkeypatch.setattr(dev["subprocess"], "run", run)
    assert not prepare(config(), "private.yaml")
    run.assert_not_called()
    assert "task app:migrate CONFIG=private.yaml" in capsys.readouterr().err


def test_failed_preflight_launches_no_development_processes(dev, monkeypatch):
    main = dev["main"]
    monkeypatch.setattr(dev["sys"], "argv", ["dev.py"])
    monkeypatch.setitem(main.__globals__, "load_config", lambda _path: config())
    monkeypatch.setitem(main.__globals__, "prepare_database", lambda *_args: False)
    launch = Mock()
    monkeypatch.setattr(dev["subprocess"], "Popen", launch)
    assert main() == 1
    launch.assert_not_called()
