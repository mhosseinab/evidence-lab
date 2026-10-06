"""Strict configuration, active-profile validation and safe provenance.

Only ``load_config`` reads operator-selected credential references. Mock mode
never resolves those references or performs network/model work. Use
``AppConfig.safe_dict`` for any external representation, never ``model_dump``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, ValidationError, field_validator, model_validator


Protocol = Literal["embeddings", "chat_completions", "cloudflare_clef"]
Role = Literal["embeddings", "generator", "verifier", "repair_generator", "evaluation_judge"]
Phase = Literal["ingestion", "queries", "smoke", "evaluation"]
NonNegativeMoney = Annotated[float, Field(ge=0)]
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
MAX_CONFIG_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024
REDACTED = "[REDACTED]"


class ConfigError(ValueError):
    """An operator-facing error whose message contains no configuration values."""

    code = "invalid_configuration"


class _SettingError(ValueError):
    """Messages of this internal exception are fixed, safe explanations."""


class _StrictConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True,
        validate_default=True,
    )


class RuntimeConfig(_StrictConfig):
    mode: Literal["mock", "live"] = "mock"
    embedding_mode: Literal["mock", "live"] | None = None
    credentials: Literal["server", "browser"] = "server"
    require_openai_compatible: bool = True
    remote_concurrency: int = Field(default=4, ge=1, le=64)
    query_deadline_seconds: float = Field(default=60, gt=0, le=3600)
    max_remote_attempts_per_query: int = Field(default=10, ge=1, le=100)
    ingestion_deadline_seconds: float = Field(default=1800, gt=0, le=86400)
    max_remote_attempts_per_ingestion: int = Field(default=2000, ge=1, le=100000)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    worker_lease_seconds: int = Field(default=120, ge=10, le=3600)
    poll_seconds: float = Field(default=0.5, gt=0, le=60)
    operator_token: SecretStr | None = Field(default=None, repr=False)

    @field_validator("operator_token")
    @classmethod
    def nonempty_operator_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise _SettingError("Operator token must contain non-whitespace characters.")
        return value

    @property
    def effective_embedding_mode(self) -> Literal["mock", "live"]:
        return "mock" if self.mode == "mock" else self.embedding_mode or self.mode


class DatabaseConfig(_StrictConfig):
    dsn: str = Field(default="postgresql://evidence:evidence@localhost:5432/evidence_lab", repr=False)

    @field_validator("dsn")
    @classmethod
    def postgres_only(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in {"postgresql", "postgres"} and bool(parsed.hostname) and bool(parsed.path.strip("/"))
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise _SettingError("database.dsn must be a PostgreSQL connection URL.")
        return value


class MemoryConfig(_StrictConfig):
    enabled: bool = True
    max_turns: int = Field(default=6, ge=1, le=20)
    max_context_bytes: int = Field(default=8000, ge=256, le=32000)


class LangSmithConfig(_StrictConfig):
    enabled: bool = False
    capture_content: bool = False
    project: str = Field(default="evidence-lab", min_length=1, max_length=96)
    api_url: str = "https://api.smith.langchain.com"
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_key_env: str | None = Field(default=None, repr=False)
    workspace_id: str | None = None
    timeout_seconds: float = Field(default=2, gt=0, le=10)

    @model_validator(mode="after")
    def explicit_tracing(self) -> LangSmithConfig:
        if not self.enabled:
            return self
        parsed = urlsplit(self.api_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise _SettingError("LangSmith requires an explicit HTTP(S) API URL without credentials, queries or fragments.")
        if sum(value is not None for value in (self.api_key, self.api_key_env)) != 1:
            raise _SettingError("Enabled LangSmith requires exactly one API key or explicit environment reference.")
        value = self.api_key.get_secret_value() if self.api_key else self.api_key_env
        if not value or not value.strip() or (self.api_key and _placeholder(value)):
            raise _SettingError("LangSmith credentials must be nonempty.")
        if self.api_key and any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
            raise _SettingError("LangSmith API keys must contain printable ASCII characters.")
        return self


class IngestionConfig(_StrictConfig):
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_pdf_pages: int = Field(default=200, ge=1)
    max_documents: int = Field(default=100, ge=1)
    max_active_chunks: int = Field(default=10000, ge=1)
    max_retained_payload_bytes: int = Field(default=5 * 1024**3, ge=1)
    inactive_retention_days: int = Field(default=30, ge=1)
    failed_staging_retention_days: int = Field(default=7, ge=1)
    max_extracted_chars: int = Field(default=10_000_000, ge=1)
    max_page_extracted_chars: int = Field(default=1_000_000, ge=1)
    chunk_target_chars: int = Field(default=1600, ge=1)
    chunk_max_chars: int = Field(default=2400, ge=1)
    chunk_overlap_chars: int = Field(default=200, ge=0)
    embedding_batch_size: int = Field(default=16, ge=1, le=16)

    @model_validator(mode="after")
    def coherent_chunking(self) -> IngestionConfig:
        if not self.chunk_overlap_chars < self.chunk_target_chars <= self.chunk_max_chars:
            raise _SettingError("Chunk overlap must be smaller than target, and target no larger than maximum.")
        if self.max_page_extracted_chars > self.max_extracted_chars:
            raise _SettingError("Per-page extraction limit cannot exceed the document extraction limit.")
        return self

    @property
    def max_file_bytes(self) -> int:
        return self.max_upload_bytes


class RetrievalConfig(_StrictConfig):
    dense_candidates: int = Field(default=40, ge=0, le=1000)
    lexical_candidates: int = Field(default=40, ge=0, le=1000)
    rrf_constant: int = Field(default=60, ge=1)
    evidence_chunks: int = Field(default=8, ge=1, le=64)
    search_mode: Literal["exact"] = "exact"

    @model_validator(mode="after")
    def some_candidates(self) -> RetrievalConfig:
        if self.dense_candidates + self.lexical_candidates == 0:
            raise _SettingError("At least one retrieval branch must request candidates.")
        return self

    @property
    def dense_limit(self) -> int:
        return self.dense_candidates

    @property
    def lexical_limit(self) -> int:
        return self.lexical_candidates

    @property
    def rrf_k(self) -> int:
        return self.rrf_constant


class VerificationConfig(_StrictConfig):
    mode: Literal["shadow", "evaluation", "verified", "gated"] = "shadow"
    policy_id: str | None = None
    policy_path: str | None = None
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    max_answer_blocks: int = Field(default=8, ge=1, le=8)
    max_answer_bytes: int = Field(default=8000, ge=512, le=64000)
    max_repair_bytes: int = Field(default=16384, ge=512, le=128000)
    max_content_repairs: int = Field(default=1, ge=0, le=1)
    evidence_policy: Literal["frozen"] = "frozen"


class BudgetConfig(_StrictConfig):
    total_max_estimated_cost_usd: NonNegativeMoney = 0
    phase_max_estimated_cost_usd: dict[Phase, NonNegativeMoney] = Field(
        default_factory=lambda: {"ingestion": 0, "queries": 0, "smoke": 0, "evaluation": 0}
    )

    @field_validator("phase_max_estimated_cost_usd", mode="before")
    @classmethod
    def disabled_missing_phases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {"ingestion": 0, "queries": 0, "smoke": 0, "evaluation": 0, **value}


class Capabilities(_StrictConfig):
    structured_output: Literal["json_schema", "json_object", "text_json"] = "json_schema"
    output_limit_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_completion_tokens"
    temperature: bool = False


class Pricing(_StrictConfig):
    input_usd_per_million: NonNegativeMoney
    output_usd_per_million: NonNegativeMoney = 0
    checked_on: str

    @field_validator("checked_on")
    @classmethod
    def dated_price(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise _SettingError("Pricing checked_on must be an ISO date string.") from None
        if parsed.isoformat() != value:
            raise _SettingError("Pricing checked_on must use YYYY-MM-DD.")
        return value


class Profile(_StrictConfig):
    protocol: Protocol
    endpoint: str | None = Field(default=None, repr=False)
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_key_env: str | None = Field(default=None, repr=False)
    api_key_file: str | None = Field(default=None, repr=False)
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer"
    model: str | None = None
    semantic_revision: str | None = None
    embedding_space: str | None = None
    dimensions: int | None = Field(default=None, ge=1, le=16000)
    request_dimensions: bool = False
    batch_size: int = Field(default=16, ge=1, le=2048)
    capabilities: Capabilities | None = None
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_batch_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_questions: int = Field(default=64, ge=1, le=64)
    token_counting: Literal["conservative_utf8_bytes"] = "conservative_utf8_bytes"
    context_headroom_fraction: float = Field(default=0.15, ge=0, lt=1)
    timeout_seconds: float = Field(default=30, gt=0, le=3600)
    max_attempts: int = Field(default=2, ge=1, le=2)
    concurrency: int = Field(default=4, ge=1, le=64)
    pricing: Pricing | None = None

    @property
    def usable_input_tokens(self) -> int:
        """Conservative input allowance before output and application reserves."""
        if self.max_input_tokens is None:
            return 0
        return math.floor(self.max_input_tokens * (1 - self.context_headroom_fraction))

    @property
    def effective_max_batch_input_tokens(self) -> int:
        return self.max_batch_input_tokens or self.max_input_tokens or 0

    def input_token_bound(self, payload: Any) -> int:
        return input_token_bound(payload)


class Roles(_StrictConfig):
    embeddings: str
    generator: str
    verifier: str
    repair_generator: str | None = None
    evaluation_judge: str | None = None


class EvaluationConfig(_StrictConfig):
    max_remote_attempts: int = Field(default=3000, ge=1)
    dataset_dir: str = "data"
    allowlisted_datasets: dict[str, str] = Field(default_factory=lambda: {"demo": "data/demo/dataset.json"})
    gold_path: str | None = None
    policy_output_dir: str = "policies"


def _placeholder(value: str | None) -> bool:
    if value is None or not value.strip():
        return True
    lowered = value.strip().lower()
    return (
        lowered.startswith(("replace", "your_", "your-", "<", "fixture:", "mock:"))
        or "replace" in lowered
        or lowered in {"todo", "changeme", "change_me", "placeholder"}
    )


def _validate_live_endpoint(value: str | None) -> None:
    if _placeholder(value):
        raise _SettingError("An active live profile requires a complete, non-placeholder endpoint URL.")
    try:
        parsed = urlsplit(value or "")
        valid = (
            parsed.scheme in {"https", "http"} and bool(parsed.hostname)
            and parsed.path not in {"", "/"} and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and not any(c.isspace() for c in (value or ""))
            and not (parsed.hostname or "").endswith((".example", ".invalid"))
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise _SettingError("Active endpoints must be complete HTTP(S) operation URLs without URL credentials, queries or fragments.")


class AgentRagConfig(_StrictConfig):
    allowed_corpora: list[str] = Field(default_factory=lambda: ["default"], min_length=1, max_length=100)
    allowed_hosts: list[str] = Field(default_factory=lambda: [
        "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*",
    ], min_length=1, max_length=100)
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:*", "http://127.0.0.1:*"])
    max_request_bytes: int = Field(default=65536, ge=1024, le=1024 * 1024)
    max_result_bytes: int = Field(default=65536, ge=1024, le=1024 * 1024)

    @field_validator("allowed_corpora")
    @classmethod
    def corpus_identifiers(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in values):
            raise _SettingError("Agent RAG corpus identifiers must be valid corpus names.")
        return values


class AppConfig(_StrictConfig):
    _source_directory: Path | None = PrivateAttr(default=None)
    config_version: Literal[1] = 1
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    agent_rag: AgentRagConfig = Field(default_factory=AgentRagConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    profiles: dict[str, Profile] = Field(min_length=1)
    roles: Roles
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)

    def role_name(self, role: Role) -> str:
        if role not in Roles.model_fields:
            raise ConfigError("Unknown pipeline role.")
        name = getattr(self.roles, role)
        if role == "repair_generator" and name is None:
            name = self.roles.generator
        if name is None or name not in self.profiles:
            raise ConfigError("A requested pipeline role has no configured profile.")
        return name

    def role_profile(self, role: Role) -> Profile:
        return self.profiles[self.role_name(role)]

    def with_roles(self, **updates: str | None) -> AppConfig:
        """Activate operator-selected profiles in a validated independent copy.

        An inactive environment/file reference stays unread until its profile is
        selected. Keep the original config directory private so relative secret
        files retain their meaning during an explicitly selected comparison.
        This does not alter the OpenAI-compatibility requirement or model limits.
        """
        if not set(updates) <= set(Roles.model_fields):
            raise ConfigError("Unknown pipeline role.")
        data = self.model_dump(mode="python")
        data["roles"].update(updates)
        try:
            selected = AppConfig.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(_safe_validation_message(exc)) from None
        selected._source_directory = self._source_directory
        if selected.runtime.mode == "live" and self._source_directory is None:
            if any(profile.api_key_file and not Path(profile.api_key_file).expanduser().is_absolute()
                   for name, profile in selected.profiles.items() if name in selected.active_profile_names()):
                raise ConfigError("Relative API key files require configuration loaded from a file.")
        _resolve_keys(selected, self._source_directory or Path.cwd())
        return selected

    def active_profile_names(self) -> set[str]:
        return {name for name in self.roles.model_dump().values() if name is not None}

    @model_validator(mode="after")
    def active_contracts(self) -> AppConfig:
        if any(not PROFILE_NAME.fullmatch(name) for name in self.profiles):
            raise _SettingError("Profile names must be short letters, numbers, periods, underscores or hyphens.")
        allowed_protocols = {
            "embeddings": {"embeddings"},
            "generator": {"chat_completions"},
            "verifier": {"chat_completions", "cloudflare_clef"},
            "repair_generator": {"chat_completions"},
            "evaluation_judge": {"chat_completions", "cloudflare_clef"},
        }
        for role, name in self.roles.model_dump().items():
            if name is None:
                continue
            if name not in self.profiles:
                raise _SettingError("An active role references a missing profile.")
            profile = self.profiles[name]
            if profile.protocol not in allowed_protocols[role]:
                raise _SettingError("An active role uses an incompatible profile protocol.")
            if self.runtime.require_openai_compatible and profile.protocol == "cloudflare_clef":
                raise _SettingError("An active Clef profile requires require_openai_compatible=false.")

        # The mock uses these shapes for database dimensions and context packing;
        # endpoint strings and credentials are not used or resolved in this mode.
        cost_configured = self.budgets.total_max_estimated_cost_usd > 0 or any(
            value > 0 for value in self.budgets.phase_max_estimated_cost_usd.values()
        )
        for name in sorted(self.active_profile_names()):
            profile = self.profiles[name]
            if profile.max_input_tokens is None:
                raise _SettingError("Active profiles require a declared max_input_tokens.")
            if profile.protocol == "embeddings":
                if profile.dimensions is None or not profile.embedding_space or not profile.model:
                    raise _SettingError("Active embedding profiles require dimensions, model and embedding_space.")
            if profile.protocol == "chat_completions":
                if profile.max_output_tokens is None or profile.capabilities is None:
                    raise _SettingError("Active chat profiles require max_output_tokens and explicit capabilities.")
                if profile.max_output_tokens >= profile.usable_input_tokens:
                    raise _SettingError("Chat output reservation must fit below the usable context limit.")
            if profile.protocol == "cloudflare_clef":
                if profile.model not in {"clef", "clef-flash"}:
                    raise _SettingError("Active native Clef profiles require body model clef or clef-flash.")
                if profile.max_input_tokens > 65536:
                    raise _SettingError("Active native Clef input limits cannot exceed the documented 65536-token context.")
                if profile.max_output_tokens is not None:
                    raise _SettingError("Native Clef profiles do not support a max_output_tokens parameter.")
                if name == self.roles.verifier and profile.max_questions < self.verification.max_answer_blocks + 3:
                    raise _SettingError("The active native verifier must fit all answer-block and three global checks in one batch.")
            if self.runtime.mode == "live" and self.runtime.credentials == "browser":
                if any(value is not None for value in (profile.api_key, profile.api_key_env, profile.api_key_file)):
                    raise _SettingError("Browser credentials must not be configured on the server.")
            if self.runtime.mode != "live" or (profile.protocol == "embeddings" and self.runtime.effective_embedding_mode == "mock"):
                continue
            _validate_live_endpoint(profile.endpoint)
            if _placeholder(profile.model):
                raise _SettingError("An active live profile requires a non-placeholder model identifier.")
            if profile.protocol == "embeddings" and _placeholder(profile.embedding_space):
                raise _SettingError("An active live embedding profile requires a non-placeholder space identifier.")
            if cost_configured and profile.pricing is None:
                raise _SettingError("Every active live profile requires dated pricing when any spending cap is nonzero.")
            if self.runtime.credentials == "browser":
                continue
            supplied = sum(value is not None for value in (profile.api_key, profile.api_key_env, profile.api_key_file))
            if supplied != 1:
                raise _SettingError("Active live profiles require exactly one API key source: direct, environment or file.")
            if profile.api_key is not None and _placeholder(profile.api_key.get_secret_value()):
                raise _SettingError("An active live profile requires a nonempty, non-placeholder API key.")
            if profile.api_key is not None and any(ord(ch) < 32 or ord(ch) > 126 for ch in profile.api_key.get_secret_value()):
                raise _SettingError("An active API key must contain only printable ASCII header characters.")
            if profile.api_key_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", profile.api_key_env):
                raise _SettingError("An active API key environment reference must be a valid variable name.")
            if profile.api_key_file is not None and not profile.api_key_file.strip():
                raise _SettingError("An active API key file reference must be nonempty.")
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", profile.auth_header):
                raise _SettingError("Authentication header name is invalid.")
            if any(ord(ch) < 32 or ord(ch) > 126 for ch in profile.auth_prefix):
                raise _SettingError("Authentication prefix must be printable ASCII.")
        # Gated eligibility is checked by the policy subsystem against its
        # manifest and held-out evidence; configuration alone never qualifies it.
        return self

    def safe_dict(self) -> dict[str, Any]:
        """Redact credentials, credential locations, private DSN and URL auth.

        Key/reference rotation and database credentials intentionally do not
        change the sanitized fingerprint. Endpoints otherwise remain visible.
        """
        data = self.model_dump(mode="json")
        data["database"]["dsn"] = REDACTED
        data["runtime"]["operator_token"] = REDACTED if self.runtime.operator_token is not None else None
        data["langsmith"]["api_url"] = _safe_endpoint(self.langsmith.api_url)
        for key in ("api_key", "api_key_env"):
            data["langsmith"][key] = REDACTED if getattr(self.langsmith, key) is not None else None
        for name, profile in self.profiles.items():
            item = data["profiles"][name]
            for key in ("api_key", "api_key_env", "api_key_file"):
                item[key] = None if self.runtime.credentials == "browser" else (REDACTED if getattr(profile, key) is not None else None)
            item["endpoint"] = _safe_endpoint(profile.endpoint)
        return data

    def fingerprint(self) -> str:
        raw = json.dumps(self.safe_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def input_token_bound(self, payload: Any) -> int:
        return input_token_bound(payload)


def _safe_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return "[UNCONFIGURED_ENDPOINT]"
        # Remove both URL userinfo and all query/fragment values, even for
        # inactive examples that are never subjected to endpoint validation.
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "[INVALID_ENDPOINT]"


def input_token_bound(payload: Any) -> int:
    """Conservative client-side UTF-8 budget, without downloaded tokenizers.

    Count complete serialized text/JSON plus a small framing reserve. Adapters
    must count their final request and apply profile headroom/output reserves.
    This is not evidence that a hosted service did not truncate its input.
    """
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    if isinstance(payload, bytes):
        return len(payload) + 64
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(payload.encode("utf-8")) + 64


class _UniqueSafeLoader(yaml.SafeLoader):
    def __init__(self, stream: str):
        super().__init__(stream)
        self._alias_count = 0
        self._node_count = 0

    def compose_node(self, parent, index):
        self._node_count += 1
        if self._node_count > 20000:
            raise ConfigError("Configuration contains too many YAML nodes.")
        if self.check_event(yaml.AliasEvent):
            self._alias_count += 1
            if self._alias_count > 100:
                raise ConfigError("Configuration contains too many YAML aliases.")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise ConfigError("Configuration must use YAML mappings.")
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConfigError("Configuration mapping keys must be strings.")
            if key in result:
                raise ConfigError("Duplicate YAML key; implicit or explicit overwrites are not permitted.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _safe_validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False, include_url=False)
    fixed_messages = []
    for error in errors:
        inner = error.get("ctx", {}).get("error")
        if isinstance(inner, _SettingError) and str(inner) not in fixed_messages:
            fixed_messages.append(str(inner))
    if fixed_messages:
        return "Configuration validation failed. " + " ".join(fixed_messages[:4])
    kinds = sorted({str(error["type"]) for error in errors})
    return "Configuration validation failed; check required fields, strict types and supported settings (" + ", ".join(kinds) + ")."


def _resolve_keys(config: AppConfig, directory: Path) -> None:
    if config.runtime.mode == "mock" or config.runtime.credentials == "browser":
        return
    for name in config.active_profile_names():
        profile = config.profiles[name]
        if profile.protocol == "embeddings" and config.runtime.effective_embedding_mode == "mock":
            continue
        if profile.api_key_env is not None:
            value = os.environ.get(profile.api_key_env)
            if value is None or _placeholder(value):
                raise ConfigError("An active API key environment reference is unset, empty or still a placeholder.")
            profile.api_key = SecretStr(value)
            # Keep exactly one resolved source so a validated config round trip
            # does not become an ambiguous two-source configuration.
            profile.api_key_env = None
        elif profile.api_key_file is not None:
            try:
                candidate = Path(profile.api_key_file).expanduser()
                if not candidate.is_absolute():
                    candidate = directory / candidate
                with candidate.open("rb") as handle:
                    raw = handle.read(MAX_SECRET_BYTES + 1)
                if len(raw) > MAX_SECRET_BYTES:
                    raise ConfigError("An active API key file exceeds the permitted size.")
                value = raw.decode("utf-8").strip()
            except (OSError, UnicodeError, ValueError):
                raise ConfigError("An active API key file could not be read as a bounded UTF-8 secret.") from None
            if _placeholder(value):
                raise ConfigError("An active API key file is empty or still a placeholder.")
            profile.api_key = SecretStr(value)
            profile.api_key_file = None
        if profile.api_key is not None:
            value = profile.api_key.get_secret_value()
            if any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
                raise ConfigError("An active API key must contain only printable ASCII header characters.")


def load_config(path: str | Path) -> AppConfig:
    """Load one private YAML file without disclosing its values in failures."""
    try:
        config_path = Path(path).expanduser()
        with config_path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigError("Configuration file exceeds the permitted size.")
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueSafeLoader)
        if not isinstance(data, dict):
            raise ConfigError("Configuration root must be a YAML mapping.")
        config = AppConfig.model_validate(data)
        config._source_directory = config_path.resolve().parent
        _resolve_keys(config, config_path.resolve().parent)
        if config.langsmith.enabled and config.langsmith.api_key_env is not None:
            value = os.environ.get(config.langsmith.api_key_env)
            if value is None or _placeholder(value) or any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
                raise ConfigError("The selected LangSmith API key environment reference is unset or invalid.")
            config.langsmith.api_key = SecretStr(value)
            config.langsmith.api_key_env = None
        return config
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(_safe_validation_message(exc)) from None
    except (yaml.YAMLError, UnicodeError, OSError, RecursionError, ValueError):
        raise ConfigError("Configuration could not be read as valid UTF-8 YAML.") from None
