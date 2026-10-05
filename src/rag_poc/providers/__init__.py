"""Configured hosted adapters and explicit deterministic fixtures.

ProviderHub is the only interface needed by ingestion, retrieval and the gate.
It does not download models and never chooses a fallback endpoint.
"""
from __future__ import annotations

import math
from typing import Any

from pydantic import ValidationError

from rag_poc.config import AppConfig, input_token_bound
from rag_poc.domain import AnswerBlock, CallContext, Draft, EvidencePack, GLOBAL_IDS, ProviderError, VerificationResult
from . import clef, mock, openai
from .prompts import compact_json
from .transport import CallExecutor, check_payload, payload_limit


def _bytes(value: Any) -> int:
    try:
        return len(compact_json(value).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ProviderError("invalid_response", "Request content cannot be serialized safely") from None


class ProviderHub:
    def __init__(self, config: AppConfig, store: Any = None, client: Any = None):
        self.config = config
        self.store = store
        self.executor = CallExecutor(config, store=store, client=client)

    async def aclose(self) -> None:
        await self.executor.aclose()

    def _generation_payload(self, profile: Any, question: str, evidence: EvidencePack, repair: dict | None) -> dict:
        settings = self.config.verification
        return openai.generation_payload(profile, question, evidence, repair, max_blocks=settings.max_answer_blocks, max_answer_bytes=settings.max_answer_bytes)

    def _verification_payload(self, profile: Any, question: str, draft: Draft, evidence: EvidencePack) -> dict:
        builders = {"chat_completions": openai.verification_payload, "cloudflare_clef": clef.verification_payload}
        builder = builders.get(profile.protocol)
        if builder is None:
            raise ProviderError("provider_unavailable", "The selected profile cannot verify answers")
        return builder(profile, question, draft, evidence)

    def _validate_draft_size(self, draft: Draft) -> Draft:
        settings = self.config.verification
        if len(draft.blocks) > settings.max_answer_blocks or _bytes(draft.model_dump(mode="json")) > settings.max_answer_bytes:
            raise ProviderError("over_budget", "The complete answer exceeds the configured block or byte limit; nothing was truncated")
        return draft

    def _validate_repair(self, repair: dict | None) -> None:
        if repair is None:
            return
        if not isinstance(repair, dict) or set(repair) != {"failed_checks", "original_draft"}:
            raise ProviderError("invalid_response", "Repair data must contain only failed check IDs and the original draft")
        failed = repair["failed_checks"]
        if not isinstance(failed, list) or not failed or any(not isinstance(item, str) for item in failed):
            raise ProviderError("invalid_response", "Repair data has invalid failed check IDs")
        if len(set(failed)) != len(failed) or len(failed) > 11:
            raise ProviderError("invalid_response", "Repair data has duplicate or excess failed check IDs")
        if _bytes(repair) > self.config.verification.max_repair_bytes:
            raise ProviderError("over_budget", "The complete repair data exceeds its configured byte allowance")
        try:
            original = Draft.model_validate(repair["original_draft"])
        except (ValidationError, ValueError, TypeError):
            raise ProviderError("invalid_response", "Repair data contains an invalid original draft") from None
        self._validate_draft_size(original)
        expected = {block.block_id for block in original.blocks} | set(GLOBAL_IDS)
        if any(item not in expected for item in failed):
            raise ProviderError("invalid_response", "Repair data contains unknown failed check IDs")

    def shared_evidence_budget(self, question: str) -> int:
        """Maximum compact serialized EvidencePack bytes for every query stage.

        Reserve actual enforced draft/repair byte limits, all prompts/schemas,
        maximum block/global questions and output limits. JSON nested inside chat
        message text may double its escaped length, hence the factor of two.
        Every real request is checked again; no answer or evidence is truncated.
        """
        settings = self.config.verification
        empty = EvidencePack(corpus_id="", space_id="", corpus_revision=0, items=[])
        empty_bytes = _bytes(empty.model_dump(mode="json"))
        draft = Draft(blocks=[AnswerBlock(block_id=f"b{i + 1}", text="x", citation_ids=["e1"]) for i in range(settings.max_answer_blocks)])
        draft_bytes = _bytes(draft.model_dump(mode="json"))
        budgets = []
        generator = self.config.role_profile("generator")
        initial = self._generation_payload(generator, question, empty, None)
        initial_overhead = input_token_bound(initial) - 2 * empty_bytes
        budgets.append(math.floor((payload_limit(generator) - initial_overhead) / 2))
        if settings.max_content_repairs:
            profile = self.config.role_profile("repair_generator")
            base = self._generation_payload(profile, question, empty, {})
            overhead = input_token_bound(base) - 2 * empty_bytes - 2 * _bytes({})
            budgets.append(math.floor((payload_limit(profile) - overhead - 2 * settings.max_repair_bytes) / 2))
        verifier = self.config.role_profile("verifier")
        base = self._verification_payload(verifier, question, draft, empty)
        multiplier = 2 if verifier.protocol == "chat_completions" else 1
        overhead = input_token_bound(base) - multiplier * empty_bytes - multiplier * draft_bytes
        # Chat repeats expected IDs separately from the answer. A domain block
        # ID may contain 64 escaped characters; reserve the complete worst case.
        id_reserve = settings.max_answer_blocks * (64 * 12 + 8) if multiplier == 2 else 0
        budgets.append(math.floor((payload_limit(verifier) - overhead - multiplier * settings.max_answer_bytes - id_reserve) / multiplier))
        budget = min(budgets)
        if budget <= empty_bytes:
            raise ProviderError("over_budget", "The selected profiles cannot fit the bounded answer, repair, and evidence contracts")
        return budget

    async def embed(self, texts: list[str], ctx: CallContext) -> list[list[float]]:
        if not isinstance(texts, list) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ProviderError("invalid_response", "Embedding inputs must be nonempty strings")
        if not texts:
            return []
        profile = self.config.role_profile("embeddings")
        name = self.config.role_name("embeddings")
        for text in texts:
            if input_token_bound(text) > profile.usable_input_tokens:
                raise ProviderError("over_budget", "An embedding input exceeds its declared limit; nothing was truncated")
        batches: list[list[str]] = []
        batch: list[str] = []
        for text in texts:
            candidate = batch + [text]
            candidate_body = openai.embedding_payload(profile, candidate)
            if batch and (len(candidate) > profile.batch_size or input_token_bound(candidate_body) > payload_limit(profile, embedding_batch=True)):
                batches.append(batch)
                batch = [text]
            else:
                batch = candidate
            # Validate even a single input before any batch is transmitted.
            check_payload(profile, openai.embedding_payload(profile, batch), embedding_batch=True)
        if batch:
            batches.append(batch)
        result: list[list[float]] = []
        for batch in batches:
            body = openai.embedding_payload(profile, batch)
            if self.config.runtime.mode == "mock":
                vectors = await self.executor.invoke_mock(name, profile, body, ctx, lambda batch=batch: mock.embed(batch, profile.dimensions, profile.model), embedding_batch=True)
            else:
                vectors = await self.executor.invoke(name, profile, body, ctx, lambda data, size=len(batch): openai.parse_embeddings(data, size, profile.dimensions), embedding_batch=True)
            result.extend(vectors)
        return result

    async def generate(self, question: str, evidence: EvidencePack, ctx: CallContext, repair: dict | None = None) -> Draft:
        self._validate_repair(repair)
        role = "repair_generator" if repair is not None else "generator"
        profile = self.config.role_profile(role)
        name = self.config.role_name(role)
        body = self._generation_payload(profile, question, evidence, repair)
        if self.config.runtime.mode == "mock":
            return await self.executor.invoke_mock(name, profile, body, ctx, lambda: self._validate_draft_size(mock.generate(question, evidence, repair, self.config.verification.max_answer_blocks)))
        settings = self.config.verification
        return await self.executor.invoke(name, profile, body, ctx, lambda data: self._validate_draft_size(openai.parse_generation(
            data, max_blocks=settings.max_answer_blocks, max_answer_bytes=settings.max_answer_bytes)), format_retry=True)

    async def generate_candidate(self, question: str, evidence: EvidencePack, ctx: CallContext) -> dict[str, Any]:
        """Evaluation A's universally decodable candidate, never a released draft.

        Interactive generation still validates Draft strictly. The evaluator is
        responsible for applying schema/citation gates to variants B, C and D.
        """
        profile = self.config.role_profile("generator")
        name = self.config.role_name("generator")
        settings = self.config.verification
        body = self._generation_payload(profile, question, evidence, None)
        options = {"max_blocks": settings.max_answer_blocks, "max_answer_bytes": settings.max_answer_bytes}
        if self.config.runtime.mode == "mock":
            return await self.executor.invoke_mock(name, profile, body, ctx, lambda: openai.candidate_from_decoded(
                mock.generate(question, evidence, None, settings.max_answer_blocks).model_dump(mode="json"), **options))
        return await self.executor.invoke(name, profile, body, ctx, lambda data: openai.parse_candidate(data, **options), format_retry=True)

    async def verify(self, question: str, draft: Draft, evidence: EvidencePack, ctx: CallContext, round_id: str = "initial") -> VerificationResult:
        self._validate_draft_size(draft)
        ids = [item.id for item in evidence.items]
        if len(set(ids)) != len(ids) or any(citation not in set(ids) for block in draft.blocks for citation in block.citation_ids):
            raise ProviderError("invalid_response", "The exact answer references missing or ambiguous evidence IDs")
        profile = self.config.role_profile("verifier")
        name = self.config.role_name("verifier")
        body = self._verification_payload(profile, question, draft, evidence)
        if self.config.runtime.mode == "mock":
            return await self.executor.invoke_mock(name, profile, body, ctx, lambda: mock.verify(question, draft, evidence, round_id))
        parsers = {
            "chat_completions": lambda data: openai.parse_verification(data, draft, evidence, round_id),
            "cloudflare_clef": lambda data: clef.parse_verification(data, profile, draft, evidence, round_id),
        }
        return await self.executor.invoke(name, profile, body, ctx, parsers[profile.protocol])


__all__ = ["ProviderHub"]
