"""OpenAI-compatible chat-completions and float-embedding contracts.

No endpoint construction, model selection, implicit parameter discovery or fallback.
"""
from __future__ import annotations

import math
from typing import Any

from pydantic import ValidationError

from rag_poc.domain import CheckResult, Draft, EvidencePack, GLOBAL_IDS, ProviderError, VerificationResult, stable_hash, strict_json
from .prompts import CHECK_SCHEMA, compact_json, generation_messages, verification_messages


def _invalid(message: str = "Provider response does not satisfy the configured contract", *, retryable: bool = False) -> ProviderError:
    return ProviderError("invalid_response", message, retryable=retryable)


def chat_payload(profile: Any, messages: list[dict[str, str]], schema: dict, name: str) -> dict:
    capabilities = profile.capabilities
    if capabilities.structured_output != "json_schema":
        messages = [dict(message) for message in messages]
        messages[0]["content"] += "\nRequired JSON schema: " + compact_json(schema)
    body: dict[str, Any] = {"model": profile.model, "messages": messages, "stream": False}
    body[capabilities.output_limit_parameter] = profile.max_output_tokens
    if capabilities.temperature:
        body["temperature"] = 0
    if capabilities.structured_output == "json_schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
    elif capabilities.structured_output == "json_object":
        body["response_format"] = {"type": "json_object"}
    return body


def generation_payload(profile: Any, question: str, evidence: EvidencePack, repair: dict | None, *, max_blocks: int, max_answer_bytes: int) -> dict:
    messages = generation_messages(question, evidence, repair, max_blocks=max_blocks, max_answer_bytes=max_answer_bytes)
    return chat_payload(profile, messages, Draft.model_json_schema(), "grounded_answer")


def verification_payload(profile: Any, question: str, draft: Draft, evidence: EvidencePack) -> dict:
    return chat_payload(profile, verification_messages(question, draft, evidence), CHECK_SCHEMA, "grounding_checks")


def embedding_payload(profile: Any, texts: list[str]) -> dict:
    result = {"model": profile.model, "input": texts, "encoding_format": "float"}
    if profile.request_dimensions:
        result["dimensions"] = profile.dimensions
    return result


def parse_chat_content(data: Any, *, retryable_format: bool = False) -> Any:
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1:
        raise _invalid()
    choice = data["choices"][0]
    if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
        raise _invalid()
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _invalid()
    if message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
        raise _invalid("Provider refused the task or returned an unsupported tool response")
    if choice.get("finish_reason") != "stop":
        raise _invalid("Provider output did not finish normally")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _invalid("Provider returned no answer content", retryable=retryable_format)
    try:
        return strict_json(content)
    except (ValueError, TypeError, RecursionError):
        raise _invalid("Provider returned invalid JSON", retryable=retryable_format) from None


def parse_generation(data: Any, *, max_blocks: int = 8, max_answer_bytes: int = 8000) -> Draft:
    # Runtime and evaluation stop at the same first universally decoded answer.
    # Schema failures are terminal; they cannot select a different runtime draft
    # that was absent from the controlled A–D comparison.
    candidate = parse_candidate(data, max_blocks=max_blocks, max_answer_bytes=max_answer_bytes)
    try:
        return Draft.model_validate(candidate["decoded"])
    except (ValidationError, ValueError, TypeError):
        raise _invalid("Provider answer failed schema validation") from None


def parse_candidate(data: Any, *, max_blocks: int, max_answer_bytes: int) -> dict[str, Any]:
    """Evaluation-only universal decoding, before variant B's schema gate.

    The display contract recovers blocks[].text. IDs, citations and extra fields
    remain unmodified in decoded so A does not silently inherit B's schema gate.
    """
    decoded = parse_chat_content(data, retryable_format=True)
    return candidate_from_decoded(decoded, max_blocks=max_blocks, max_answer_bytes=max_answer_bytes)


def candidate_from_decoded(decoded: Any, *, max_blocks: int, max_answer_bytes: int) -> dict[str, Any]:
    blocks = decoded.get("blocks") if isinstance(decoded, dict) else None
    if not isinstance(blocks, list) or not blocks:
        raise _invalid("Candidate answer has no recoverable block text", retryable=True)
    if len(blocks) > max_blocks or len(compact_json(decoded).encode("utf-8")) > max_answer_bytes:
        raise ProviderError("over_budget", "The complete candidate exceeds the configured block or byte limit; nothing was truncated")
    if any(not isinstance(block, dict) or not isinstance(block.get("text"), str) or not block["text"].strip() for block in blocks):
        raise _invalid("Candidate answer has no recoverable block text", retryable=True)
    return {"text": "\n\n".join(block["text"] for block in blocks), "decoded": decoded, "candidate_hash": stable_hash(decoded)}


def validate_coverage(checks: list[CheckResult], draft: Draft) -> None:
    expected = {b.block_id: "block_support" for b in draft.blocks}
    expected.update({key: "global" for key in GLOBAL_IDS})
    actual = [check.id for check in checks]
    if len(actual) != len(expected) or len(set(actual)) != len(actual) or set(actual) != set(expected):
        raise ProviderError("incomplete_coverage", "Verifier did not cover every expected check exactly once")
    if any(check.kind != expected[check.id] for check in checks):
        raise ProviderError("incomplete_coverage", "Verifier returned a check with an unexpected kind")


def parse_verification(data: Any, draft: Draft, evidence: EvidencePack, round_id: str) -> VerificationResult:
    decoded = parse_chat_content(data)
    if not isinstance(decoded, dict) or set(decoded) != {"checks"} or not isinstance(decoded["checks"], list):
        raise _invalid("Verifier response failed schema validation")
    checks: list[CheckResult] = []
    required = {"id", "kind", "support_status", "check_status", "reason"}
    try:
        for row in decoded["checks"]:
            # Chat labels deliberately cannot smuggle synthetic scores into the gate.
            if not isinstance(row, dict) or set(row) != required:
                raise ValueError("Unexpected check fields")
            checks.append(CheckResult.model_validate(row))
    except (ValidationError, ValueError, TypeError):
        raise _invalid("Verifier response failed schema validation") from None
    validate_coverage(checks, draft)
    return VerificationResult(
        checks=checks, answer_hash=draft.content_hash, evidence_hash=evidence.content_hash,
        round_id=round_id, raw={"schema": "chat-labels-v1", "score_semantics": "labels_only", "checks": decoded["checks"]},
    )


def parse_embeddings(data: Any, count: int, dimensions: int) -> list[list[float]]:
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != count:
        raise _invalid("Embedding response has missing or extra vectors")
    result: list[list[float] | None] = [None] * count
    for row in rows:
        if not isinstance(row, dict) or type(row.get("index")) is not int:
            raise _invalid("Embedding response has invalid indices")
        index = row["index"]
        if index < 0 or index >= count or result[index] is not None:
            raise _invalid("Embedding response has duplicate or foreign indices")
        vector = row.get("embedding")
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise _invalid("Embedding response has incompatible dimensions")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise _invalid("Embedding response contains a non-finite or non-numeric value")
        converted = [float(value) for value in vector]
        norm = math.hypot(*converted)
        if not math.isfinite(norm) or norm == 0:
            raise _invalid("Embedding response contains an invalid or zero vector")
        result[index] = converted
    if any(vector is None for vector in result):
        raise _invalid("Embedding response has missing vectors")
    return [vector for vector in result if vector is not None]
