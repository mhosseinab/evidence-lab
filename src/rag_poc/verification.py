"""Reproducible engineering verification; never model inference or model quality.

The native command uses only databases it creates through an explicitly supplied
RAG_TEST_NATIVE_ADMIN_DSN. It never selects the application's database for tests.
The ephemeral PGlite test runner is supported, but cannot establish native
concurrency or backup/restore. Missing coverage and skipped tests stay visible.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import quote, quote_plus, urlencode
from xml.etree import ElementTree

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from .domain import stable_hash, strict_json
from .evaluation import implementation_fingerprint
from .policy import semantic_policy_fingerprint
from .storage import Store


def _project_root():
    # Wheels live in site-packages; Docker and checkout commands run in a
    # project directory containing the separately shipped tests and fixtures.
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    return next((path for path in candidates if (path / "pyproject.toml").is_file()
                 and (path / "tests").is_dir()), candidates[-1])


ROOT = _project_root()
SUITE = (
    "tests/test_config.py", "tests/test_providers.py", "tests/test_ingestion.py",
    "tests/test_retrieval.py", "tests/test_storage.py", "tests/test_engine.py",
    "tests/test_api.py", "tests/test_evaluation.py", "tests/test_worker.py", "tests/test_experiments.py",
    "tests/test_native_restore.py", "tests/test_verification.py", "tests/test_upload_body_limit.py",
)
RESTORE_TEST = "tests/test_native_restore.py::test_native_pg_dump_restore_preserves_sources_vectors_and_retrieval"
NATIVE_TESTS = (
    "tests/test_storage.py::TestNativeMultiSessionConcurrency::test_two_workers_cannot_claim_same_job",
    "tests/test_storage.py::TestNativeMultiSessionConcurrency::test_competing_reservations_cannot_overrun_global_spend",
    "tests/test_storage.py::TestNativeMultiSessionConcurrency::test_dense_and_lexical_keep_one_snapshot_during_concurrent_activation",
)


def _selectors(module, *names):
    return tuple(f"tests/test_{module}.py::{name}" for name in names)


# These are explicit observable contracts, not inferred line-coverage claims.
# A renamed/deleted test leaves missing coverage until this manifest is reviewed.
REQUIRED_CHECKS = {
    "configuration": _selectors("config", "test_live_rejects_incomplete_active_profiles",
        "test_role_errors_are_explicit", "test_duplicate_yaml_key_rejected_without_echoing_secret",
        "test_unknown_parameters_are_rejected_not_passed_to_remote_api"),
    "protocol_boundary": _selectors("config", "test_active_native_protocol_requires_explicit_opt_in",
        "test_inactive_incomplete_profile_is_not_resolved_or_validated_live")
        + _selectors("providers", "test_chat_capabilities_and_no_unsupported_temperature",
            "test_native_clef_exact_route_choice_schema_reason_and_uncalibrated_scores"),
    "authentication_network": _selectors("providers", "test_status_retries_are_bounded_and_errors_are_sanitized",
        "test_network_error_counts_each_attempt_without_leaking_request",
        "test_per_call_timeout_and_run_deadline_prevent_late_release",
        "test_cancelled_context_never_sends_and_inflight_cancel_records_ledger"),
    "embeddings": _selectors("providers", "test_embedding_reorders_indices_and_sends_exact_configured_operation",
        "test_invalid_embedding_vectors_fail_without_retry", "test_nonfinite_embedding_json_is_rejected"),
    "generation": _selectors("providers", "test_generation_format_retry_is_bounded_and_accounted",
        "test_refusal_length_and_tool_response_are_not_retried"),
    "verification_coverage": _selectors("providers", "test_chat_verdict_coverage_and_score_injection_fail_closed",
        "test_native_clef_malformed_contract_is_not_retried")
        + _selectors("engine", "test_incomplete_or_ambiguous_checks_are_technical_failures",
            "test_stale_verification_metadata_never_releases_an_answer"),
    "grounding": _selectors("providers", "test_mock_quote_support_and_same_evidence_repair_never_use_http",
        "test_mock_conflict_requires_second_attributed_source")
        + _selectors("evaluation", "test_controlled_original_claims_are_checked_without_repair",
            "test_demo_controlled_fixture_labels_cover_eight_mutation_families"),
    "repair": _selectors("engine", "test_unsupported_answer_has_one_repair_and_rechecks_unchanged_blocks",
        "test_second_semantic_failure_abstains_without_a_third_draft",
        "test_initial_round_verdict_cannot_approve_an_unchanged_repair",
        "test_verifier_outage_does_not_trigger_repair_or_semantic_abstention"),
    "input_budget": _selectors("providers", "test_every_embedding_input_is_preflighted_before_first_batch",
        "test_full_prompt_budget_checks_suffix_without_local_truncation",
        "test_native_suffix_evidence_and_all_eleven_checks_are_transmitted",
        "test_small_verifier_contract_rejects_shared_evidence_budget",
        "test_unknown_citation_and_oversized_answer_fail_before_call")
        + _selectors("upload_body_limit", "test_chunked_upload_stops_reading_at_body_limit",
            "test_chunked_multipart_returns_safe_http_413"),
    "adversarial_data": _selectors("providers", "test_hostile_evidence_and_draft_remain_data_without_tools_or_instruction_override",
        "test_repair_metadata_is_exact_bounded_and_cannot_override_system",
        "test_refusal_length_and_tool_response_are_not_retried"),
    "source_index_state": _selectors("storage",
        "TestPostgresPersistence::test_upload_is_idempotent_by_bytes_and_pipeline_with_atomic_job",
        "TestPostgresPersistence::test_staged_embedding_batches_resume_after_reclaim_without_losing_cache",
        "TestPostgresPersistence::test_failed_new_version_leaves_previous_active_retrieval_available",
        "TestPostgresPersistence::test_invalid_embedding_batch_rolls_back_every_vector",
        "TestPostgresPersistence::test_extracted_content_vectors_and_embedding_space_are_immutable"),
    "snapshot_lease_races": _selectors("retrieval", "test_space_change_during_query_embedding_aborts_instead_of_mixing_vectors")
        + _selectors("storage", "TestPostgresPersistence::test_late_source_worker_cannot_write_renew_or_finish_after_reclaim",
            "TestPostgresPersistence::test_latest_requested_version_wins_even_if_older_embeddings_finish_first",
            "TestPostgresPersistence::test_query_cancellation_fences_late_publication_events_and_future_calls")
        + NATIVE_TESTS,
    "retrieval": _selectors("retrieval", "test_rrf_counts_once_per_branch_preserves_ranks_and_breaks_ties_by_id",
        "test_empty_branches_are_valid_and_no_fake_candidates_are_added",
        "test_overlap_dedupe_keeps_adjacent_chunks_with_substantial_new_content",
        "test_identical_text_from_distinct_sources_keeps_both_provenances")
        + _selectors("storage", "TestPostgresPersistence::test_dense_and_native_fts_share_active_source_and_corpus_filters")
        + _selectors("ingestion", "test_postgres_mock_upload_retrieval_update_and_cited_verdict_round_trip"),
    "trace_retention": _selectors("api", "test_public_run_and_list_hide_unverified_drafts_while_trace_is_labelled",
        "test_status_and_health_never_return_config_credentials_or_trigger_inference")
        + _selectors("storage", "TestPostgresPersistence::test_metadata_credentials_are_redacted_in_settings_events_and_call_records",
            "TestPostgresPersistence::test_explicit_source_purge_cancels_holding_query_and_preserves_cost_ledger",
            "TestPostgresPersistence::test_cleanup_preserves_referenced_old_source_then_removes_it_after_run_retention",
            "TestPostgresPersistence::test_upload_and_document_quotas_fail_without_partial_jobs"),
    "evaluation_accounting": _selectors("evaluation", "test_failed_and_abstained_cases_stay_in_fixed_denominators",
        "test_restarted_evaluation_is_incomplete_without_replaying_calls",
        "test_report_rejects_dropped_rows_and_changed_gold_metadata")
        + _selectors("storage", "TestPostgresPersistence::test_unknown_cost_and_attempt_caps_survive_store_recreation",
            "TestPostgresPersistence::test_mock_ledger_cannot_spend_money_or_reset_attempt_caps",
            "TestPostgresPersistence::test_profile_concurrency_is_shared_across_store_instances_and_releases_on_completion"),
    "recovery": (RESTORE_TEST,) + _selectors("storage", "TestPostgresPersistence::test_migration_is_repeatable_and_pgvector_schema_is_live")
        + _selectors("worker", "test_crash_after_terminal_publication_recovers_without_repeat_inference",
            "test_heartbeat_lease_loss_cancels_operation_without_stale_publication"),
}


def _source_manifest():
    paths = [path for path in (ROOT / "src").rglob("*") if path.is_file()
             and path.suffix in {".py", ".js", ".html", ".css", ".svg"}]
    paths += [ROOT / value for value in SUITE]
    paths += [ROOT / value for value in ("pyproject.toml", "configs/mock.yaml", "scripts/backup.py", "scripts/restore.py")]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths)) if path.is_file()}


def _checkout_implementation_fingerprint():
    root = ROOT / "src" / "rag_poc"
    files = [path for path in root.rglob("*") if path.is_file()
             and path.suffix in (".py", ".js", ".html", ".css", ".txt")
             and "__pycache__" not in path.parts]
    return stable_hash({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(files)})


def _git_revision():
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, timeout=5, check=False)
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _secret_values(config):
    values = [config.database.dsn]
    for name in ("RAG_TEST_DSN", "RAG_TEST_NATIVE_ADMIN_DSN"):
        if os.environ.get(name):
            values.append(os.environ[name])
    for dsn in list(values):
        try:
            password = conninfo_to_dict(dsn).get("password")
            if password:
                values.append(password)
        except psycopg.Error:
            pass
    for profile in config.profiles.values():
        if profile.api_key:
            values.append(profile.api_key.get_secret_value())
    if config.runtime.operator_token:
        values.append(config.runtime.operator_token.get_secret_value())
    values = {str(value) for value in values if value}
    values |= {quote(value, safe="") for value in values} | {quote_plus(value, safe="") for value in values}
    return sorted(values, key=len, reverse=True)


def _redact(text, secrets):
    for secret in secrets:
        # Local fixture passwords can be ordinary words such as "postgres".
        # Do not mutate node IDs containing that word as part of an identifier.
        if len(secret) < 16 and re.fullmatch(r"[\w-]+", secret):
            text = re.sub(r"(?<![\w-])" + re.escape(secret) + r"(?![\w-])", "[REDACTED]", text)
        else:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(postgres(?:ql)?://[^\s:/]+:)[^@\s]+(@)", r"\1[REDACTED]\2", text)


def _database_identity(dsn):
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        postgres = connection.execute("SHOW server_version").fetchone()[0]
        vector = connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        migration = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return {"postgres_version": postgres, "pgvector_version": vector[0] if vector else None,
            "migration_revision": migration[0] if migration else None}


def _isolated_database_url(admin_dsn, database_name):
    options = conninfo_to_dict(admin_dsn)
    if not options.get("host"):
        raise ValueError("Native test administration requires an explicit host")
    copied = {key: value for key, value in options.items() if key != "dbname"}
    url = "postgresql://localhost/" + quote(database_name, safe="") + "?" + urlencode(copied)
    selected = conninfo_to_dict(url)
    if selected.get("dbname") != database_name or any(selected.get(key) != value for key, value in copied.items()):
        raise ValueError("Could not construct an isolated test database connection")
    return url


@contextmanager
def _test_environment(native):
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUNBUFFERED"] = "1"
    info = {"backend": "offline", "setup_errors": [], "cleanup_errors": [],
            "postgres_version": None, "pgvector_version": None, "migration_revision": None}
    if env.get("RAG_TEST_BACKEND", "").lower() == "pglite" and env.get("RAG_TEST_DSN"):
        info["backend"] = "pglite"
        env.pop("RAG_TEST_NATIVE_ADMIN_DSN", None)
        try:
            Store(env["RAG_TEST_DSN"]).migrate()
            info.update(_database_identity(env["RAG_TEST_DSN"]))
        except Exception:
            info["setup_errors"].append("ephemeral_sql_database_unavailable")
            env.pop("RAG_TEST_DSN", None)
        yield env, info
        return
    admin_dsn = env.get("RAG_TEST_NATIVE_ADMIN_DSN") if native else None
    env.pop("RAG_TEST_DSN", None)
    env.pop("RAG_TEST_NATIVE_ADMIN_DSN", None)
    env["RAG_TEST_BACKEND"] = "offline"
    created, test_identity, test_name = False, None, "rag_verify_" + uuid4().hex
    if admin_dsn:
        try:
            test_dsn = _isolated_database_url(admin_dsn, test_name)
            with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as connection:
                connection.execute("SET statement_timeout='20s'")
                connection.execute("SET lock_timeout='5s'")
                connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(test_name)))
                created = True
                test_identity = connection.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (test_name,)).fetchone()
                if not test_identity:
                    raise ValueError("Created database identity could not be verified")
            with psycopg.connect(test_dsn, connect_timeout=10) as connection:
                if connection.execute("SELECT current_database()").fetchone()[0] != test_name:
                    raise ValueError("Test database selection did not match the created database")
            Store(test_dsn).migrate()
            info.update(_database_identity(test_dsn))
            info["backend"] = "native_postgresql"
            env["RAG_TEST_DSN"] = test_dsn
            env["RAG_TEST_BACKEND"] = "native"
            env["RAG_TEST_NATIVE_ADMIN_DSN"] = admin_dsn
        except Exception:
            info["setup_errors"].append("native_disposable_database_setup_failed")
    elif native:
        info["setup_errors"].append("native_admin_dsn_not_supplied")
    try:
        yield env, info
    finally:
        if created:
            try:
                with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as connection:
                    # Only this invocation's freshly created disposable database.
                    current = connection.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (test_name,)).fetchone()
                    if current is not None:
                        if test_identity is None or current != test_identity:
                            raise ValueError("Temporary database identity changed; refusing removal")
                        connection.execute("SET statement_timeout='20s'")
                        connection.execute("SET lock_timeout='5s'")
                        connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(test_name)))
            except (psycopg.Error, ValueError):
                info["cleanup_errors"].append("temporary_test_database_cleanup_failed")
                info["temporary_database_name"] = test_name


def _read_junit(raw):
    root = ElementTree.fromstring(raw)
    records = []
    for case in root.iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        filename = case.get("file")
        parts = classname.split(".")
        module_index = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
        if not filename and module_index is not None:
            filename = "/".join(parts[:module_index + 1]) + ".py"
        classes = parts[module_index + 1:] if module_index is not None else []
        node_id = "::".join([filename or "unknown", *classes, name])
        failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        status = "failed" if failure is not None else "error" if error is not None else "skipped" if skipped is not None else "passed"
        record = {"node_id": node_id, "status": status, "duration_seconds": float(case.get("time", "0"))}
        if skipped is not None:
            record["skip_reason"] = skipped.get("message", "Test skipped")
        records.append(record)
    return records


def _redact_junit(raw, secrets):
    # Decode XML first so credentials containing '&' or quotes are still
    # removed, then let the serializer escape the sanitized text correctly.
    root = ElementTree.fromstring(raw)
    for element in root.iter():
        if element.text:
            element.text = _redact(element.text, secrets)
        if element.tail:
            element.tail = _redact(element.tail, secrets)
        for key, value in list(element.attrib.items()):
            element.set(key, _redact(value, secrets))
    return ElementTree.tostring(root, encoding="unicode")


def _coverage(records):
    checks = {}
    for area, selectors in REQUIRED_CHECKS.items():
        matching, missing = [], []
        for selector in selectors:
            found = [item for item in records if item["node_id"] == selector or item["node_id"].startswith(selector + "[")]
            if not found:
                missing.append(selector)
            matching.extend(found)
        statuses = {item["status"] for item in matching}
        status = "failed" if statuses & {"failed", "error"} else "missing" if missing else "skipped" if "skipped" in statuses else "passed"
        checks[area] = {"status": status, "test_ids": sorted({item["node_id"] for item in matching}),
                        "missing_test_ids": missing, "scope": "engineering_fixture_contracts"}
    return checks


def _write_private(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)


def verify_environment(config, output_dir, *, native=False, report_path=None):
    """Run the fixed fault suite and return paths plus an honest gate summary.

    ``report_path`` optionally binds the result to one evaluation report. Native
    success requires the full suite with zero skips and actual restore evidence;
    it still establishes no live endpoint performance or semantic reliability.
    """
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact_id = str(uuid4())
    stem = "fault-suite-" + artifact_id
    semantic = semantic_policy_fingerprint(config)
    implementation = implementation_fingerprint()
    runtime_matches_checkout = implementation == _checkout_implementation_fingerprint()
    manifest = _source_manifest()
    secrets = _secret_values(config)
    binding, reasons = {}, []
    if not runtime_matches_checkout:
        reasons.append("installed_runtime_does_not_match_tested_checkout")
    if report_path is not None:
        try:
            report_file = Path(report_path).expanduser().resolve()
            if report_file.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Report too large")
            report_raw = report_file.read_bytes()
            report = strict_json(report_raw.decode("utf-8"))
            identity = report.get("identity", {})
            if (not report.get("id") or not report.get("dataset_hash") or identity.get("policy_hash") != semantic
                    or identity.get("implementation_fingerprint") != implementation):
                raise ValueError("Evaluation identity mismatch")
            binding = {"evaluation_id": report["id"], "dataset_hash": report["dataset_hash"],
                       "evaluation_report_sha256": hashlib.sha256(report_raw).hexdigest()}
        except (OSError, ValueError, TypeError, AttributeError):
            reasons.append("evaluation_report_missing_invalid_or_identity_mismatch")
    else:
        reasons.append("evaluation_report_not_supplied")
    missing_files = [name for name in SUITE if not (ROOT / name).is_file()]
    available_suite = [name for name in SUITE if (ROOT / name).is_file()]
    harness_errors, returncode, records, raw_xml = [], None, [], ""
    started = time.monotonic()
    transcript = ""
    with tempfile.TemporaryDirectory(prefix="rag-verify-") as temporary:
        junit = Path(temporary) / "pytest.xml"
        with _test_environment(bool(native)) as (env, database):
            command = [sys.executable, "-m", "pytest", *available_suite, "-q", "-ra", "--tb=short",
                       "-o", "junit_family=xunit1", "--junitxml", str(junit)]
            if not available_suite:
                harness_errors.append("test_sources_unavailable")
            else:
                try:
                    result = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                            timeout=600, check=False)
                    returncode, transcript = result.returncode, result.stdout
                except subprocess.TimeoutExpired as exc:
                    harness_errors.append("test_suite_timeout")
                    transcript = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
                except OSError:
                    harness_errors.append("test_runner_unavailable")
            if junit.is_file():
                try:
                    raw_xml = _redact_junit(junit.read_text(encoding="utf-8"), secrets)
                    records = _read_junit(raw_xml)
                except (ElementTree.ParseError, ValueError):
                    harness_errors.append("test_report_invalid")
            else:
                harness_errors.append("test_report_missing")
    harness_errors.extend(database["setup_errors"] + database["cleanup_errors"])
    if missing_files:
        reasons.append("mandatory_test_files_missing")
    unchanged = manifest == _source_manifest() and implementation == implementation_fingerprint()
    if not unchanged:
        reasons.append("source_changed_during_verification")
    summary = {"passed": sum(row["status"] == "passed" for row in records),
               "failed": sum(row["status"] == "failed" for row in records),
               "skipped": sum(row["status"] == "skipped" for row in records),
               "errors": sum(row["status"] == "error" for row in records) + len(harness_errors)}
    checks = _coverage(records)
    passed_ids = {row["node_id"] for row in records if row["status"] == "passed"}
    native_verified = bool(native and database["backend"] == "native_postgresql"
                           and all(name in passed_ids for name in NATIVE_TESTS)
                           and database["migration_revision"] and database["pgvector_version"]
                           and not harness_errors)
    restore_verified = bool(native_verified and RESTORE_TEST in passed_ids)
    complete = bool(returncode == 0 and summary["passed"] > 0 and not any(summary[key] for key in ("failed", "skipped", "errors"))
                    and not missing_files and unchanged and runtime_matches_checkout
                    and all(check["status"] == "passed" for check in checks.values()))
    if not native_verified:
        reasons.append("native_postgresql_not_verified")
    if not restore_verified:
        reasons.append("native_backup_restore_not_verified")
    if not complete:
        reasons.append("mandatory_fault_suite_incomplete")
    transcript = _redact(transcript, secrets)
    xml_path, log_path, artifact_path = (output / (stem + suffix) for suffix in (".junit.xml", ".log.txt", ".json"))
    _write_private(xml_path, raw_xml.encode("utf-8"))
    _write_private(log_path, transcript.encode("utf-8"))
    artifact = {
        "schema_version": 1, "kind": "fault_suite", "artifact_id": artifact_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_id": None, "dataset_hash": None, **binding,
        "semantic_fingerprint": semantic, "implementation_fingerprint": implementation,
        "source_revision": _git_revision(), "source_manifest": manifest,
        "source_manifest_hash": stable_hash(manifest), "source_unchanged": unchanged,
        "runtime_matches_tested_checkout": runtime_matches_checkout,
        "runtime_mode": config.runtime.mode, "native_requested": bool(native),
        "backend": database["backend"], "native_postgres_verified": native_verified,
        "restore_verified": restore_verified, "migration_revision": database["migration_revision"],
        "postgres_version": database["postgres_version"], "pgvector_version": database["pgvector_version"],
        "complete": complete, "qualification_eligible": bool(complete and binding and native_verified and restore_verified),
        "model_semantics_measured": False, "live_endpoint_performance_measured": False,
        "qualification_note": "Fixtures validate engineering contracts. This artifact alone cannot qualify a model or live policy.",
        "summary": summary, "checks": checks, "tests": records, "suite_files": list(SUITE),
        "missing_files": missing_files, "harness_errors": harness_errors, "reasons": reasons,
        "temporary_database_name": database.get("temporary_database_name"),
        "returncode": returncode, "duration_seconds": round(time.monotonic() - started, 3),
        "evidence_files": {"junit": {"file": xml_path.name, "sha256": hashlib.sha256(raw_xml.encode("utf-8")).hexdigest()},
                           "transcript": {"file": log_path.name, "sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest()}},
    }
    artifact["content_hash"] = stable_hash(artifact)
    _write_private(artifact_path, (json.dumps(artifact, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    return {"artifact_path": str(artifact_path), "junit_path": str(xml_path), "transcript_path": str(log_path),
            "summary": summary, "complete": complete, "qualification_eligible": artifact["qualification_eligible"],
            "native_postgres_verified": native_verified, "restore_verified": restore_verified, "reasons": reasons}
