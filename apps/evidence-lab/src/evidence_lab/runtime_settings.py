"""Validate browser runtime choices without changing the server configuration."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from evidence_lab.config import AppConfig, ConfigError, _validate_live_endpoint


class LiveSettings(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", allow_inf_nan=False,
        hide_input_in_errors=True, validate_default=True,
    )

    embedding_source: Literal["workspace", "custom"] = "custom"
    embedding_endpoint: str | None = Field(default=None, min_length=1, max_length=2048)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=256)
    embedding_dimensions: int | None = Field(default=None, ge=1, le=16000)
    embedding_max_input_tokens: int | None = Field(default=None, ge=1)
    chat_endpoint: str = Field(min_length=1, max_length=2048)
    chat_model: str = Field(min_length=1, max_length=256)
    chat_max_input_tokens: int = Field(ge=1)
    chat_max_output_tokens: int = Field(ge=1)
    structured_output: Literal["json_schema", "json_object", "text_json"]
    output_limit_parameter: Literal["max_tokens", "max_completion_tokens"]
    cloudflare_account_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")
    budget_usd: float = Field(default=0, ge=0)
    embedding_input_usd_per_million: float | None = Field(default=None, ge=0)
    chat_input_usd_per_million: float = Field(ge=0)
    chat_output_usd_per_million: float = Field(ge=0)

    @field_validator("embedding_endpoint", "chat_endpoint")
    @classmethod
    def complete_endpoint(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_live_endpoint(value)
        return value

    @model_validator(mode="after")
    def explicit_custom_embeddings(self) -> LiveSettings:
        if self.embedding_source == "custom" and any(value is None for value in (
            self.embedding_endpoint, self.embedding_model, self.embedding_dimensions,
            self.embedding_max_input_tokens, self.embedding_input_usd_per_million,
        )):
            raise ValueError("Custom embeddings require complete settings.")
        return self


def _live_profiles(settings: LiveSettings, config: AppConfig) -> dict[str, dict]:
    checked_on = date.today().isoformat()
    identity = json.dumps([
        settings.embedding_endpoint, settings.embedding_model, settings.embedding_dimensions,
    ], separators=(",", ":"))
    space = "byok-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    profiles = {
        "byok-embeddings": {
            "protocol": "embeddings", "endpoint": settings.embedding_endpoint,
            "model": settings.embedding_model, "embedding_space": space,
            "dimensions": settings.embedding_dimensions, "request_dimensions": False,
            "batch_size": 16, "max_input_tokens": settings.embedding_max_input_tokens,
            "max_batch_input_tokens": settings.embedding_max_input_tokens,
            "pricing": {"input_usd_per_million": settings.embedding_input_usd_per_million,
                        "output_usd_per_million": 0, "checked_on": checked_on},
        },
        "byok-chat": {
            "protocol": "chat_completions", "endpoint": settings.chat_endpoint,
            "model": settings.chat_model, "max_input_tokens": settings.chat_max_input_tokens,
            "max_output_tokens": settings.chat_max_output_tokens,
            "capabilities": {"structured_output": settings.structured_output,
                             "output_limit_parameter": settings.output_limit_parameter,
                             "temperature": False},
            "pricing": {"input_usd_per_million": settings.chat_input_usd_per_million,
                        "output_usd_per_million": settings.chat_output_usd_per_million,
                        "checked_on": checked_on},
        },
        "byok-clef": {
            "protocol": "cloudflare_clef",
            "endpoint": ("https://api.cloudflare.com/client/v4/accounts/"
                         f"{settings.cloudflare_account_id}/ai/run/@cf/cloudflare/clef"),
            "model": "clef", "max_input_tokens": 65536,
            "max_questions": 64, "context_headroom_fraction": 0.25,
            "pricing": {"input_usd_per_million": 0.24, "output_usd_per_million": 0,
                        "checked_on": checked_on},
        },
    }
    if settings.embedding_source == "workspace":
        embedding = config.role_profile("embeddings").model_dump(mode="python")
        for field in ("api_key", "api_key_env", "api_key_file"):
            embedding[field] = None
        profiles["byok-embeddings"] = embedding
    return profiles


def select_runtime(config: AppConfig, mode_header: str | None, settings_header: str | None) -> AppConfig:
    if mode_header is None or (mode_header == "mock" and config.runtime.mode == "mock"):
        return config
    if mode_header not in {"mock", "live"}:
        raise ConfigError("Choose mock or live mode in workspace connection.")
    try:
        data = config.model_dump(mode="python")
        data["runtime"].update(mode=mode_header, credentials="browser")
        data["verification"].update(policy_path=None, score_threshold=None)
        if mode_header == "mock":
            data["runtime"]["embedding_mode"] = None
            for profile in data["profiles"].values():
                for field in ("api_key", "api_key_env", "api_key_file"):
                    profile[field] = None
                if profile["protocol"] == "embeddings" and config.runtime.effective_embedding_mode != "mock":
                    identity = json.dumps([
                        profile["embedding_space"], profile["model"],
                        profile["endpoint"], profile["dimensions"],
                    ], separators=(",", ":"))
                    profile["embedding_space"] = "mock-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            data["verification"].update(mode="gated", policy_id="mock-fixture-only")
        else:
            if not settings_header or len(settings_header) > 16384:
                raise ValueError
            settings = LiveSettings.model_validate_json(settings_header)
            data["runtime"]["require_openai_compatible"] = False
            data["runtime"]["embedding_mode"] = (config.runtime.effective_embedding_mode
                                                   if settings.embedding_source == "workspace" else None)
            data["profiles"] = _live_profiles(settings, config)
            data["roles"] = {
                "embeddings": "byok-embeddings", "generator": "byok-chat",
                "verifier": "byok-clef", "repair_generator": None, "evaluation_judge": None,
            }
            data["budgets"] = {
                "total_max_estimated_cost_usd": settings.budget_usd,
                "phase_max_estimated_cost_usd": {
                    phase: settings.budget_usd for phase in ("ingestion", "queries", "smoke", "evaluation")
                },
            }
            data["verification"].update(mode="shadow", policy_id=None)
        selected = AppConfig.model_validate(data)
        selected._source_directory = config._source_directory
        return selected
    except (ValueError, TypeError, ValidationError):
        raise ConfigError("Complete valid live provider settings in workspace connection.") from None
