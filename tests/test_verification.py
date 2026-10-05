"""Qualification inputs must reflect observed tests, scope and source identity."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from psycopg.conninfo import conninfo_to_dict

from rag_poc.config import load_config
from rag_poc import verification


def test_fault_suite_manifest_includes_every_project_test_file():
    actual = {str(path.relative_to(verification.ROOT))
              for path in (verification.ROOT / "tests").rglob("test_*.py")}
    assert set(verification.SUITE) == actual


def test_junit_class_and_parameter_identity_and_skip_are_preserved():
    raw = '''<testsuites><testsuite><testcase file="tests/test_storage.py"
      classname="tests.test_storage.TestNativeMultiSessionConcurrency"
      name="test_two_workers_cannot_claim_same_job" time="0.2"><skipped message="native unavailable"/>
      </testcase><testcase classname="tests.test_providers" name="test_bad[duplicate]" time="0"/>
      </testsuite></testsuites>'''
    rows = verification._read_junit(raw)
    assert rows[0]["node_id"] == verification.NATIVE_TESTS[0]
    assert rows[0]["status"] == "skipped" and rows[0]["skip_reason"] == "native unavailable"
    assert rows[1]["node_id"] == "tests/test_providers.py::test_bad[duplicate]"


def test_missing_and_skipped_named_coverage_never_count_as_passed():
    checks = verification._coverage([{"node_id": verification.RESTORE_TEST, "status": "skipped"}])
    assert all(value["status"] != "passed" for value in checks.values())
    assert checks["recovery"]["missing_test_ids"]


def test_short_fixture_password_redaction_preserves_postgres_test_identifiers():
    text = "tests/test_storage.py::test_postgres_restore postgres password=postgres"
    result = verification._redact(text, ["postgres"])
    assert "test_postgres_restore" in result
    assert "password=[REDACTED]" in result
    assert " postgres " not in result


def test_junit_redaction_handles_xml_escaped_secrets_without_corrupting_the_report():
    raw = '<testsuites><testsuite><testcase name="test_example"><skipped message="key=a&amp;b"/></testcase></testsuite></testsuites>'
    safe = verification._redact_junit(raw, ["a&b"])
    assert "a&amp;b" not in safe and "[REDACTED]" in safe
    assert verification._read_junit(safe)[0]["status"] == "skipped"


def test_child_database_selection_overrides_query_dbname_and_preserves_connection_settings():
    dsn = "postgresql://tester:private%40value@db.example/original?dbname=existing&sslmode=require"
    child = verification._isolated_database_url(dsn, "rag_verify_unique")
    parsed = conninfo_to_dict(child)
    assert parsed["dbname"] == "rag_verify_unique"
    assert parsed["host"] == "db.example" and parsed["sslmode"] == "require"
    assert parsed["password"] == "private@value"


def test_native_requires_explicit_admin_and_never_connects_to_application_database(monkeypatch):
    monkeypatch.delenv("RAG_TEST_NATIVE_ADMIN_DSN", raising=False)
    monkeypatch.delenv("RAG_TEST_BACKEND", raising=False)
    monkeypatch.setenv("RAG_TEST_DSN", "postgresql://operator:secret@db.example/application")
    monkeypatch.setattr(verification.psycopg, "connect", lambda *a, **k: pytest.fail("No database connection authorized"))
    with verification._test_environment(True) as (env, info):
        assert "RAG_TEST_DSN" not in env
        assert "RAG_TEST_NATIVE_ADMIN_DSN" not in env
        assert info["backend"] == "offline"
        assert "native_admin_dsn_not_supplied" in info["setup_errors"]


def _fixture_runner(monkeypatch, *, backend="pglite", fault=None, changed=False):
    """Synthetic pytest subprocess output; these are artifact-contract tests."""
    config = load_config("configs/mock.yaml")
    monkeypatch.setattr(verification, "semantic_policy_fingerprint", lambda _: "semantic-fixture")
    monkeypatch.setattr(verification, "implementation_fingerprint", lambda: "source-fixture")
    monkeypatch.setattr(verification, "_checkout_implementation_fingerprint", lambda: "source-fixture")
    monkeypatch.setattr(verification, "_git_revision", lambda: None)
    counter = 0

    def manifest():
        nonlocal counter
        counter += 1
        return {"file": str(counter if changed else 1)}

    monkeypatch.setattr(verification, "_source_manifest", manifest)

    @contextmanager
    def environment(_native):
        yield {}, {"backend": backend, "setup_errors": [], "cleanup_errors": [],
                   "postgres_version": "fixture", "pgvector_version": "fixture", "migration_revision": "0001"}

    monkeypatch.setattr(verification, "_test_environment", environment)

    def execute(command, **kwargs):
        root = ElementTree.Element("testsuites")
        suite = ElementTree.SubElement(root, "testsuite")
        nodes = sorted({node for selectors in verification.REQUIRED_CHECKS.values() for node in selectors})
        for node in nodes:
            if fault == "missing" and node == verification.RESTORE_TEST:
                continue
            path, *names = node.split("::")
            classname = path.removesuffix(".py").replace("/", ".")
            if len(names) > 1:
                classname += "." + ".".join(names[:-1])
            case = ElementTree.SubElement(suite, "testcase", file=path, classname=classname, name=names[-1], time="0.01")
            if node == verification.RESTORE_TEST and fault in {"skipped", "failure", "error"}:
                ElementTree.SubElement(case, fault, message="synthetic qualification blocker")
        Path(command[command.index("--junitxml") + 1]).write_text(ElementTree.tostring(root, encoding="unicode"))
        return SimpleNamespace(returncode=1 if fault in {"failure", "error"} else 0, stdout="Synthetic contract test output.")

    monkeypatch.setattr(verification.subprocess, "run", execute)
    # The files must exist in an installed checkout; the full suite independently
    # observes their actual executions. Keep this contract fixture independent.
    monkeypatch.setattr(verification, "SUITE", ("tests/test_config.py",))
    return config


def _bound_report(tmp_path):
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps({"id": "evaluation-fixture", "dataset_hash": "dataset-fixture",
                               "identity": {"policy_hash": "semantic-fixture", "implementation_fingerprint": "source-fixture"}}))
    return path


def test_pglite_cannot_claim_native_or_restore_even_when_all_fixture_cases_pass(monkeypatch, tmp_path):
    config = _fixture_runner(monkeypatch)
    result = verification.verify_environment(config, tmp_path / "out", native=True, report_path=_bound_report(tmp_path))
    assert result["summary"]["passed"] > 0
    assert result["native_postgres_verified"] is False
    assert result["restore_verified"] is False
    assert result["qualification_eligible"] is False
    artifact = json.loads(Path(result["artifact_path"]).read_text())
    assert artifact["model_semantics_measured"] is False
    assert artifact["live_endpoint_performance_measured"] is False
    assert artifact["evaluation_id"] == "evaluation-fixture"


@pytest.mark.parametrize("fault", ["skipped", "failure", "error", "missing"])
def test_native_fault_artifact_cannot_qualify_with_skips_failures_or_missing_coverage(monkeypatch, tmp_path, fault):
    config = _fixture_runner(monkeypatch, backend="native_postgresql", fault=fault)
    result = verification.verify_environment(config, tmp_path / "out", native=True, report_path=_bound_report(tmp_path))
    assert result["complete"] is False and result["qualification_eligible"] is False
    artifact = json.loads(Path(result["artifact_path"]).read_text())
    assert artifact["checks"]["recovery"]["status"] != "passed"


def test_source_change_during_tests_invalidates_environment_evidence(monkeypatch, tmp_path):
    config = _fixture_runner(monkeypatch, backend="native_postgresql", changed=True)
    result = verification.verify_environment(config, tmp_path / "out", native=True, report_path=_bound_report(tmp_path))
    assert result["complete"] is False
    assert "source_changed_during_verification" in result["reasons"]


def test_unbound_environment_artifact_cannot_qualify_and_evidence_files_are_hashed(monkeypatch, tmp_path):
    config = _fixture_runner(monkeypatch, backend="native_postgresql")
    result = verification.verify_environment(config, tmp_path / "out", native=True)
    assert result["qualification_eligible"] is False
    artifact = json.loads(Path(result["artifact_path"]).read_text())
    assert artifact["evaluation_id"] is None and artifact["dataset_hash"] is None
    assert "evaluation_report_not_supplied" in artifact["reasons"]
    assert len(artifact["evidence_files"]["junit"]["sha256"]) == 64
    assert len(artifact["content_hash"]) == 64
    assert Path(result["artifact_path"]).stat().st_mode & 0o777 == 0o600


def test_installed_runtime_must_match_the_code_used_by_the_test_subprocess(monkeypatch, tmp_path):
    config = _fixture_runner(monkeypatch, backend="native_postgresql")
    monkeypatch.setattr(verification, "_checkout_implementation_fingerprint", lambda: "different-source")
    result = verification.verify_environment(config, tmp_path / "out", native=True, report_path=_bound_report(tmp_path))
    assert result["complete"] is False and result["qualification_eligible"] is False
    assert "installed_runtime_does_not_match_tested_checkout" in result["reasons"]
