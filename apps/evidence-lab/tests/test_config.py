"""Configuration boundary tests: no network, downloads, or credential exposure."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from evidence_lab.config import AppConfig, ConfigError, input_token_bound, load_config


PROJECT = Path(__file__).resolve().parents[3]


@pytest.fixture
def mock_data():
    return yaml.safe_load((PROJECT / "configs/mock.yaml").read_text())


@pytest.fixture
def live_data(mock_data):
    data = copy.deepcopy(mock_data)
    data["runtime"]["mode"] = "live"
    data["verification"]["mode"] = "shadow"
    data["verification"]["policy_id"] = None
    for role, name in data["roles"].items():
        profile = data["profiles"][name]
        profile["endpoint"] = f"https://api.service.test/v1/{'embeddings' if role == 'embeddings' else 'chat/completions'}"
        profile["api_key"] = "test-secret-valid-for-mocked-transport-only"
        profile["model"] = f"test-{role}-model-v1"
    return data


def save(tmp_path, data):
    path = tmp_path / "private.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_mock_example_has_usable_fixture_settings_and_disabled_spend():
    config = load_config(PROJECT / "configs/mock.yaml")
    assert config.runtime.mode == "mock"
    assert config.runtime.require_openai_compatible is True
    assert config.role_profile("embeddings").dimensions == 64
    assert config.role_profile("generator").max_input_tokens >= 131072
    assert config.verification.mode == "gated"
    assert config.verification.policy_id == "mock-fixture-only"
    assert config.budgets.total_max_estimated_cost_usd == 0
    assert not any(config.budgets.phase_max_estimated_cost_usd.values())
    assert config.role_name("repair_generator") == config.role_name("generator")
    assert config.ingestion.max_file_bytes == config.ingestion.max_upload_bytes
    assert config.retrieval.dense_limit == config.retrieval.dense_candidates


def test_live_example_deliberately_requires_operator_values():
    with pytest.raises(ConfigError, match="Active profiles require|active live profile"):
        load_config(PROJECT / "configs/live.example.yaml")


def test_mock_mode_does_not_read_key_files_or_environment(tmp_path, mock_data, monkeypatch):
    profile = mock_data["profiles"][mock_data["roles"]["generator"]]
    profile["api_key_file"] = "/path/that/does/not/exist.key"
    profile["api_key_env"] = "NONEXISTENT_MOCK_KEY"
    monkeypatch.delenv("NONEXISTENT_MOCK_KEY", raising=False)
    config = load_config(save(tmp_path, mock_data))
    assert config.runtime.mode == "mock"
    assert config.role_profile("generator").api_key is None


@pytest.mark.parametrize("change", ["missing_key", "placeholder_key", "missing_limit", "placeholder_url", "missing_model", "missing_dimensions", "missing_capabilities"])
def test_live_rejects_incomplete_active_profiles(tmp_path, live_data, change):
    generator = live_data["profiles"][live_data["roles"]["generator"]]
    embedding = live_data["profiles"][live_data["roles"]["embeddings"]]
    if change == "missing_key":
        generator.pop("api_key")
    elif change == "placeholder_key":
        generator["api_key"] = "REPLACE_PRIVATE_SECRET"
    elif change == "missing_limit":
        generator["max_input_tokens"] = None
    elif change == "placeholder_url":
        generator["endpoint"] = "https://REPLACE.example/v1/chat/completions"
    elif change == "missing_model":
        generator["model"] = None
    elif change == "missing_dimensions":
        embedding["dimensions"] = None
    elif change == "missing_capabilities":
        generator["capabilities"] = None
    with pytest.raises(ConfigError):
        load_config(save(tmp_path, live_data))


def test_inactive_incomplete_profile_is_not_resolved_or_validated_live(tmp_path, live_data):
    live_data["profiles"]["future-clef"] = {
        "protocol": "cloudflare_clef",
        "endpoint": "REPLACE_ENDPOINT",
        "model": "REPLACE_MODEL",
        "max_input_tokens": None,
        "api_key_file": "/nonexistent/unconfigured.key",
    }
    config = load_config(save(tmp_path, live_data))
    assert config.runtime.require_openai_compatible is True
    assert config.profiles["future-clef"].api_key is None


@pytest.mark.parametrize("reference", ["env", "file"])
def test_explicit_profile_selection_resolves_only_newly_active_reference(tmp_path, live_data, monkeypatch, reference):
    candidate = copy.deepcopy(live_data["profiles"][live_data["roles"]["verifier"]])
    candidate.pop("api_key")
    candidate["model"] = "candidate-verifier-v2"
    if reference == "env":
        candidate["api_key_env"] = "RAG_EXPLICIT_CANDIDATE_KEY"
        monkeypatch.delenv("RAG_EXPLICIT_CANDIDATE_KEY", raising=False)
    else:
        candidate["api_key_file"] = "candidate.key"
    live_data["profiles"]["candidate"] = candidate
    config = load_config(save(tmp_path, live_data))
    assert config.profiles["candidate"].api_key is None
    if reference == "env":
        monkeypatch.setenv("RAG_EXPLICIT_CANDIDATE_KEY", "new-candidate-secret")
    else:
        (tmp_path / "candidate.key").write_text("new-candidate-secret\n")
    selected = config.with_roles(verifier="candidate")
    assert selected.role_name("verifier") == "candidate"
    assert selected.role_profile("verifier").api_key.get_secret_value() == "new-candidate-secret"
    assert config.role_name("verifier") != "candidate" and config.profiles["candidate"].api_key is None
    safe = json.dumps(selected.safe_dict())
    assert "new-candidate-secret" not in safe and "candidate.key" not in safe and str(tmp_path) not in safe


def test_profile_selection_never_enables_native_compatibility_implicitly(tmp_path, live_data):
    live_data["profiles"]["native-candidate"] = {
        "protocol": "cloudflare_clef", "model": "clef", "max_input_tokens": 65536,
        "endpoint": "https://api.service.test/accounts/account/ai/run/@cf/cloudflare/clef",
        "api_key_env": "ABSENT_NATIVE_KEY_MUST_NOT_BE_RESOLVED",
    }
    config = load_config(save(tmp_path, live_data))
    with pytest.raises(ConfigError, match="require_openai_compatible=false"):
        config.with_roles(verifier="native-candidate")
    assert config.runtime.require_openai_compatible is True


def test_manually_constructed_config_does_not_guess_relative_secret_directory(live_data):
    candidate = copy.deepcopy(live_data["profiles"][live_data["roles"]["verifier"]])
    candidate.pop("api_key")
    candidate["api_key_file"] = "ambiguous-relative-secret.key"
    live_data["profiles"]["candidate"] = candidate
    config = AppConfig.model_validate(live_data)
    with pytest.raises(ConfigError, match="require configuration loaded from a file"):
        config.with_roles(verifier="candidate")


def test_profile_selection_errors_do_not_echo_unknown_values(tmp_path, live_data):
    config = load_config(save(tmp_path, live_data))
    with pytest.raises(ConfigError) as error:
        config.with_roles(verifier="private-unknown-profile-name")
    assert "private-unknown-profile-name" not in str(error.value)
    with pytest.raises(ConfigError, match="Unknown pipeline role"):
        config.with_roles(unknown="anything")


def test_active_native_protocol_requires_explicit_opt_in(tmp_path, live_data):
    live_data["profiles"]["native-verifier"] = {
        "protocol": "cloudflare_clef",
        "endpoint": "https://api.service.test/accounts/account/ai/run/@cf/cloudflare/clef",
        "model": "clef",
        "api_key": "secret-for-http-fixture",
        "max_input_tokens": 65536,
        "max_questions": 64,
    }
    live_data["roles"]["verifier"] = "native-verifier"
    with pytest.raises(ConfigError, match="require_openai_compatible=false"):
        load_config(save(tmp_path, live_data))
    live_data["runtime"]["require_openai_compatible"] = False
    config = load_config(save(tmp_path, live_data))
    assert config.role_profile("verifier").protocol == "cloudflare_clef"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("model", "unsupported-native-model", "body model"),
        ("max_input_tokens", 65537, "65536-token"),
        ("max_output_tokens", 1200, "do not support"),
        ("max_questions", 10, "one batch"),
    ],
)
@pytest.mark.parametrize("mode", ["live", "mock"])
def test_active_native_contract_cannot_overstate_provider_capabilities(tmp_path, live_data, field, value, reason, mode):
    live_data["runtime"]["mode"] = mode
    live_data["runtime"]["require_openai_compatible"] = False
    live_data["profiles"]["native-verifier"] = {
        "protocol": "cloudflare_clef",
        "endpoint": "https://api.service.test/accounts/account/ai/run/@cf/cloudflare/clef",
        "model": "clef",
        "api_key": "secret-for-http-fixture",
        "max_input_tokens": 65536,
        "max_questions": 11,
    }
    live_data["profiles"]["native-verifier"][field] = value
    live_data["roles"]["verifier"] = "native-verifier"
    with pytest.raises(ConfigError, match=reason):
        load_config(save(tmp_path, live_data))


@pytest.mark.parametrize("change", ["missing_profile", "wrong_protocol", "foreign_role"])
def test_role_errors_are_explicit(tmp_path, mock_data, change):
    if change == "missing_profile":
        mock_data["roles"]["generator"] = "not-present"
    elif change == "wrong_protocol":
        mock_data["roles"]["generator"] = mock_data["roles"]["embeddings"]
    else:
        mock_data["roles"]["web_search"] = "fixture-generator"
    with pytest.raises(ConfigError):
        load_config(save(tmp_path, mock_data))


def test_duplicate_yaml_key_rejected_without_echoing_secret(tmp_path, mock_data):
    path = save(tmp_path, mock_data)
    path.write_text(path.read_text() + "runtime: {operator_token: duplicate-secret-value}\n")
    with pytest.raises(ConfigError, match="Duplicate YAML key") as error:
        load_config(path)
    assert "duplicate-secret-value" not in str(error.value)


def test_duplicate_yaml_merge_key_is_not_silently_overwritten(tmp_path):
    path = tmp_path / "merged.yaml"
    path.write_text("a: &base {value: 1}\nb: {<<: *base, value: 2}\n")
    with pytest.raises(ConfigError, match="Duplicate YAML key"):
        load_config(path)


@pytest.mark.parametrize("setting,value", [("remote_concurrency", "4"), ("query_deadline_seconds", True), ("remote_concurrency", 0), ("remote_concurrency", float("nan"))])
def test_strict_operational_types_and_limits(tmp_path, mock_data, setting, value):
    mock_data["runtime"][setting] = value
    with pytest.raises(ConfigError):
        load_config(save(tmp_path, mock_data))


def test_unknown_parameters_are_rejected_not_passed_to_remote_api(tmp_path, mock_data):
    mock_data["profiles"][mock_data["roles"]["generator"]]["capabilities"]["arbitrary_parameter"] = True
    with pytest.raises(ConfigError, match="extra_forbidden"):
        load_config(save(tmp_path, mock_data))


def test_secret_redaction_and_key_rotation_fingerprint(tmp_path, live_data):
    key = "key-never-log-741205"
    password = "db-password-never-log-741205"
    operator = "operator-token-never-log-741205"
    live_data["runtime"]["operator_token"] = operator
    live_data["database"]["dsn"] = f"postgresql://user:{password}@localhost/db"
    for name in live_data["roles"].values():
        live_data["profiles"][name]["api_key"] = key
    config = load_config(save(tmp_path, live_data))
    safe = json.dumps(config.safe_dict())
    assert all(secret not in safe for secret in (key, password, operator))
    assert all(secret not in repr(config) for secret in (key, password, operator))
    first = config.fingerprint()
    for name in live_data["roles"].values():
        live_data["profiles"][name]["api_key"] = "rotated-key"
    changed = load_config(save(tmp_path, live_data))
    assert first == changed.fingerprint()
    live_data["profiles"][live_data["roles"]["generator"]]["model"] = "different-semantic-model"
    assert first != load_config(save(tmp_path, live_data)).fingerprint()


def test_inactive_url_credentials_and_query_are_redacted(tmp_path, mock_data):
    mock_data["profiles"]["future"] = {
        "protocol": "chat_completions",
        "endpoint": "https://user:password-hidden@api.test/v1/run?api_key=query-secret#fragment-secret",
    }
    safe = json.dumps(load_config(save(tmp_path, mock_data)).safe_dict())
    assert all(secret not in safe for secret in ("password-hidden", "query-secret", "fragment-secret"))


@pytest.mark.parametrize("suffix", ["?api_key=never-leak", "#never-leak", "/\nnever-leak"])
def test_active_urls_reject_ambiguous_or_embedded_auth(tmp_path, live_data, suffix):
    live_data["profiles"][live_data["roles"]["generator"]]["endpoint"] += suffix
    with pytest.raises(ConfigError) as error:
        load_config(save(tmp_path, live_data))
    assert "never-leak" not in str(error.value)


def test_direct_env_and_file_secrets_work_without_environment_requirement(tmp_path, live_data, monkeypatch):
    names = live_data["roles"]
    live_data["profiles"][names["embeddings"]]["api_key"] = "direct-key"
    generator = live_data["profiles"][names["generator"]]
    generator.pop("api_key")
    generator["api_key_env"] = "RAG_TEST_GENERATOR_KEY"
    monkeypatch.setenv("RAG_TEST_GENERATOR_KEY", "environment-key")
    verifier = live_data["profiles"][names["verifier"]]
    verifier.pop("api_key")
    verifier["api_key_file"] = "verifier.key"
    (tmp_path / "verifier.key").write_text("file-key\n")
    config = load_config(save(tmp_path, live_data))
    assert config.role_profile("embeddings").api_key.get_secret_value() == "direct-key"
    assert config.role_profile("generator").api_key.get_secret_value() == "environment-key"
    assert config.role_profile("verifier").api_key.get_secret_value() == "file-key"
    safe = json.dumps(config.safe_dict())
    assert all(secret not in safe for secret in ("direct-key", "environment-key", "file-key", "verifier.key"))


def test_missing_active_reference_fails_safely(tmp_path, live_data, monkeypatch):
    profile = live_data["profiles"][live_data["roles"]["generator"]]
    profile.pop("api_key")
    profile["api_key_env"] = "RAG_TEST_ABSENT_SECRET"
    monkeypatch.delenv("RAG_TEST_ABSENT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="reference is unset"):
        load_config(save(tmp_path, live_data))


def test_ambiguous_active_key_sources_are_rejected(tmp_path, live_data):
    live_data["profiles"][live_data["roles"]["generator"]]["api_key_env"] = "RAG_KEY"
    with pytest.raises(ConfigError, match="exactly one API key source"):
        load_config(save(tmp_path, live_data))


def test_bad_key_header_characters_do_not_escape_in_errors(tmp_path, live_data):
    live_data["profiles"][live_data["roles"]["generator"]]["api_key"] = "line-one-secret\r\nX-Header: secret"
    with pytest.raises(ConfigError) as error:
        load_config(save(tmp_path, live_data))
    assert "line-one-secret" not in str(error.value)


def test_yaml_and_pydantic_failures_do_not_echo_values(tmp_path, mock_data):
    path = tmp_path / "private.yaml"
    path.write_text("api_key: 'syntax-secret-never-echo\n")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "syntax-secret-never-echo" not in str(error.value)
    mock_data["runtime"]["remote_concurrency"] = "typed-secret-never-echo"
    with pytest.raises(ConfigError) as error:
        load_config(save(tmp_path, mock_data))
    assert "typed-secret-never-echo" not in str(error.value)
    with pytest.raises(ValidationError) as error:
        AppConfig.model_validate(mock_data)
    assert "typed-secret-never-echo" not in str(error.value)


def test_missing_budget_phases_remain_disabled(tmp_path, mock_data):
    mock_data["budgets"]["phase_max_estimated_cost_usd"] = {"smoke": 0.02}
    config = load_config(save(tmp_path, mock_data))
    assert config.budgets.phase_max_estimated_cost_usd == {"ingestion": 0, "queries": 0, "smoke": 0.02, "evaluation": 0}
    assert config.budgets.total_max_estimated_cost_usd == 0


@pytest.mark.parametrize("change", ["negative", "nonfinite", "unknown_phase"])
def test_invalid_budget_cannot_enable_unbounded_spend(tmp_path, mock_data, change):
    if change == "unknown_phase":
        mock_data["budgets"]["phase_max_estimated_cost_usd"]["miscellaneous"] = 100
    else:
        mock_data["budgets"]["total_max_estimated_cost_usd"] = -1 if change == "negative" else float("inf")
    with pytest.raises(ConfigError):
        load_config(save(tmp_path, mock_data))


def test_date_pricing_and_bounds(tmp_path, mock_data):
    profile = mock_data["profiles"][mock_data["roles"]["generator"]]
    profile["pricing"] = {"input_usd_per_million": 1.0, "output_usd_per_million": 2.0, "checked_on": "2026-10-05"}
    config = load_config(save(tmp_path, mock_data))
    assert config.role_profile("generator").pricing.input_usd_per_million == 1.0
    profile["pricing"]["checked_on"] = "not-a-date"
    with pytest.raises(ConfigError, match="ISO date"):
        load_config(save(tmp_path, mock_data))


@pytest.mark.parametrize("scope", ["total", "smoke"])
def test_nonzero_live_spend_settings_require_pricing_on_every_active_profile(tmp_path, live_data, scope):
    # Pricing may be omitted in a completed read-only live configuration.
    assert load_config(save(tmp_path, live_data)).role_profile("generator").pricing is None
    if scope == "total":
        live_data["budgets"]["total_max_estimated_cost_usd"] = 0.05
    else:
        live_data["budgets"]["phase_max_estimated_cost_usd"]["smoke"] = 0.05
    with pytest.raises(ConfigError, match="dated pricing"):
        load_config(save(tmp_path, live_data))
    for name in live_data["roles"].values():
        live_data["profiles"][name]["pricing"] = {
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 2.0,
            "checked_on": "2026-10-05",
        }
    assert load_config(save(tmp_path, live_data)).role_profile("generator").pricing is not None


def test_utf8_estimate_includes_payload_serialization_not_character_count():
    payload = {"text": "Ä🙂", "questions": {"q": "What?"}}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert input_token_bound(payload) == len(encoded) + 64
    assert input_token_bound("🙂") == 68
    with pytest.raises(ValueError):
        input_token_bound({"not_finite": float("nan")})


def test_no_sqlite_or_in_memory_database_fallback(tmp_path, mock_data):
    mock_data["database"]["dsn"] = "sqlite:///:memory:"
    with pytest.raises(ConfigError, match="PostgreSQL"):
        load_config(save(tmp_path, mock_data))


def test_config_rejects_incoherent_chunk_and_answer_budgets(tmp_path, mock_data):
    mock_data["ingestion"]["chunk_overlap_chars"] = 1600
    with pytest.raises(ConfigError, match="Chunk overlap"):
        load_config(save(tmp_path, mock_data))
    mock_data["ingestion"]["chunk_overlap_chars"] = 200
    mock_data["verification"]["max_content_repairs"] = 2
    with pytest.raises(ConfigError):
        load_config(save(tmp_path, mock_data))
