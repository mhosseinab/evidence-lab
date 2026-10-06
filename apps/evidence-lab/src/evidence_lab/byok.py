"""Browser keys exist only in a request-scoped provider configuration."""
from __future__ import annotations

import json

from pydantic import SecretStr

from evidence_lab.config import AppConfig, ConfigError, _placeholder

KEY_HEADER = "X-Evidence-Lab-Provider-Keys"


def _valid_key(key: object) -> bool:
    return (isinstance(key, str) and bool(key) and len(key) <= 4096
            and not _placeholder(key)
            and all(33 <= ord(ch) <= 126 for ch in key))


def _profile_keys(config: AppConfig, keys: object) -> dict[str, str]:
    if not isinstance(keys, dict):
        raise ValueError
    names = config.active_profile_names()
    if config.runtime.effective_embedding_mode == "mock":
        names.discard(config.roles.embeddings)
    # Existing API clients can still send one key per active profile.
    if set(keys) == names:
        if not all(_valid_key(key) for key in keys.values()):
            raise ValueError
        return keys
    if not set(keys) <= {"llm", "cloudflare"}:
        raise ValueError
    groups = {
        name: "cloudflare" if config.profiles[name].protocol == "cloudflare_clef" else "llm"
        for name in names
    }
    for group in set(groups.values()):
        if not _valid_key(keys.get(group)):
            raise ValueError
    for group, key in keys.items():
        if group not in groups.values() and key != "" and not _valid_key(key):
            raise ValueError
    return {name: keys[group] for name, group in groups.items()}


def request_config(config: AppConfig, header: str | None) -> AppConfig:
    if config.runtime.mode != "live" or config.runtime.credentials != "browser":
        return config
    try:
        if not header or len(header) > 24000:
            raise ValueError
        keys = _profile_keys(config, json.loads(header))
    except (ValueError, TypeError):
        raise ConfigError("Enter a valid LLM provider key and, when configured, a Cloudflare key in workspace connection.") from None
    scoped = config.model_copy(deep=True)
    for name, key in keys.items():
        scoped.profiles[name].api_key = SecretStr(key)
    return scoped
