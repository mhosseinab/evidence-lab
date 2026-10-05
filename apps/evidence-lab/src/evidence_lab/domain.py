"""Shared contracts. No vendor or persistence dependencies."""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GLOBAL_IDS = ("global.task_scope", "global.internal_consistency", "global.counterevidence")
PROMPT_VERSION = "grounded-v1"
SCHEMA_VERSION = "checks-v1"
PIPELINE_VERSION = "text-v1-chars1600-2400-200"


def stable_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def strict_json(raw: str) -> Any:
    def pairs(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = val
        return result
    def constant(value):
        raise ValueError("Non-finite JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceItem(Contract):
    id: str
    document_id: str
    version_id: str
    title: str
    text: str
    page: int = 1
    start: int = 0
    end: int = 0
    text_hash: str = ""
    dense_rank: int | None = None
    lexical_rank: int | None = None
    fused_score: float = 0.0


class EvidencePack(Contract):
    corpus_id: str
    space_id: str
    corpus_revision: int
    items: list[EvidenceItem]
    omitted_ids: list[str] = Field(default_factory=list)
    counting_method: str = "conservative_utf8_bytes"

    @property
    def content_hash(self) -> str:
        return stable_hash(self)


class AnswerBlock(Contract):
    block_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=3000)
    citation_ids: list[str] = Field(min_length=1, max_length=8)


class Draft(Contract):
    blocks: list[AnswerBlock] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_blocks(self):
        ids = [b.block_id for b in self.blocks]
        if len(ids) != len(set(ids)) or any(i.startswith("global.") for i in ids):
            raise ValueError("Duplicate or reserved block ID")
        for b in self.blocks:
            if len(b.citation_ids) != len(set(b.citation_ids)):
                raise ValueError("Duplicate citation ID")
        return self

    @property
    def content_hash(self) -> str:
        return stable_hash(self)

    def render(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


class CheckResult(Contract):
    id: str
    kind: Literal["block_support", "global"]
    support_status: Literal["supported", "not_supported"] | None = None
    check_status: Literal["pass", "fail"] | None = None
    reason: Literal["contradicted", "insufficient_evidence", "conflicting_evidence", "not_provided"] | None = None
    support_score: float | None = None
    raw_scores: dict[str, float] | None = None

    @model_validator(mode="after")
    def valid_kind(self):
        if self.kind == "block_support":
            if self.support_status is None or self.check_status is not None:
                raise ValueError("Block checks require support_status only")
            success = self.support_status == "supported"
        else:
            if self.check_status is None or self.support_status is not None:
                raise ValueError("Global checks require check_status only")
            success = self.check_status == "pass"
        if success and self.reason is not None:
            raise ValueError("Successful checks cannot have a failure reason")
        vals = list((self.raw_scores or {}).values())
        if self.support_score is not None:
            vals.append(self.support_score)
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in vals):
            raise ValueError("Scores must be finite in [0,1]")
        return self


class VerificationResult(Contract):
    checks: list[CheckResult]
    execution_status: Literal["ok", "invalid_response", "incomplete_coverage", "over_budget", "timeout", "provider_unavailable", "budget_exhausted"] = "ok"
    answer_hash: str = ""
    evidence_hash: str = ""
    round_id: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class ProviderError(Exception):
    def __init__(self, status: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class CallContext:
    run_id: str
    phase: str
    deadline: float
    max_attempts: int = 10
    attempts_used: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False

    @classmethod
    def for_seconds(cls, run_id: str, phase: str, seconds: float = 60, max_attempts: int = 10):
        return cls(run_id, phase, time.monotonic() + seconds, max_attempts)

    def remaining(self) -> float:
        if self.cancelled:
            raise ProviderError("cancelled", "Run cancelled")
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise ProviderError("timeout", "Run deadline exceeded")
        return value

    def consume_attempt(self):
        self.remaining()
        if self.attempts_used >= self.max_attempts:
            raise ProviderError("budget_exhausted", "Run attempt limit reached")
        self.attempts_used += 1
