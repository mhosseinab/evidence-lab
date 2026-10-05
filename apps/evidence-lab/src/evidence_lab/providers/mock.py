"""Deterministic plumbing fixtures. These functions are not language models.

The mock copies source excerpts and recognizes explicit adversarial fixture tags.
It cannot estimate semantic verification quality or endpoint performance.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from evidence_lab.domain import AnswerBlock, CheckResult, Draft, EvidenceItem, EvidencePack, GLOBAL_IDS, ProviderError, VerificationResult

MOCK_VERSION = "deterministic-excerpts-v1"
_WORDS = re.compile(r"\w+", re.UNICODE)
_TAG = re.compile(r"\[(?:fixture|mock):[a-z_]+\]", re.IGNORECASE)
_STOP = {"a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how", "in", "is", "it", "of", "on", "or", "the", "this", "to", "what", "when", "where", "which", "with"}


def tagged(question: str, tag: str) -> bool:
    lowered = question.casefold()
    return f"[fixture:{tag}]" in lowered or f"[mock:{tag}]" in lowered


def words(text: str) -> list[str]:
    return [token for token in _WORDS.findall(text.casefold()) if token not in _STOP]


def embed(texts: list[str], dimensions: int, model: str) -> list[list[float]]:
    output = []
    for text in texts:
        counts = Counter(words(text)) or Counter({text: 1})
        vector = [0.0] * dimensions
        for word, count in counts.items():
            digest = hashlib.sha256((model + "\0" + word).encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % dimensions
            vector[index] += (1.0 if digest[8] % 2 else -1.0) * (1.0 + math.log(count))
        norm = math.hypot(*vector)
        if norm == 0:
            vector[0] = 1.0
            norm = 1.0
        output.append([value / norm for value in vector])
    return output


def _excerpt(item: EvidenceItem, question: str) -> tuple[str, float]:
    query_words = set(words(_TAG.sub("", question)))
    candidates = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n\s*\n", item.text) if part.strip()]
    if not candidates:
        return "", -1.0
    ranked = []
    for index, part in enumerate(candidates):
        overlap = len(query_words & set(words(part)))
        ranked.append((float(overlap) + 0.001 / (index + 1), part))
    score, excerpt = max(ranked, key=lambda row: row[0])
    if len(excerpt) > 1200:
        boundary = excerpt.rfind(" ", 0, 1200)
        excerpt = excerpt[:boundary if boundary > 0 else 1200]
    return excerpt, score


def generate(question: str, evidence: EvidencePack, repair: dict | None, max_blocks: int) -> Draft:
    candidates = []
    for index, item in enumerate(evidence.items):
        excerpt, score = _excerpt(item, question)
        if excerpt:
            candidates.append((score, -index, item, excerpt))
    if not candidates:
        raise ProviderError("invalid_response", "The mock generator requires nonempty quoted evidence")
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    chosen = [candidates[0]]
    if tagged(question, "conflict") and repair is not None and max_blocks >= 2:
        first_document = candidates[0][2].document_id
        other = next((row for row in candidates[1:] if row[2].document_id != first_document), None)
        if other:
            chosen.append(other)
    blocks = []
    for index, (_, _, item, excerpt) in enumerate(chosen):
        text = f'{item.title}: "{excerpt}"'
        if tagged(question, "unsupported") and repair is None:
            text += " The source additionally guarantees 987654321 free upgrades for every account."
        blocks.append(AnswerBlock(block_id=f"b{index + 1}", text=text, citation_ids=[item.id]))
    return Draft(blocks=blocks)


def _supported(text: str, sources: list[EvidenceItem]) -> bool:
    for item in sources:
        if text in item.text:
            return True
        prefix = f'{item.title}: "'
        if text.startswith(prefix) and text.endswith('"'):
            excerpt = text[len(prefix):-1]
            if excerpt and excerpt in item.text:
                return True
    return False


def verify(question: str, draft: Draft, evidence: EvidencePack, round_id: str) -> VerificationResult:
    by_id = {item.id: item for item in evidence.items}
    checks: list[CheckResult] = []
    for block in draft.blocks:
        sources = [by_id[citation] for citation in block.citation_ids if citation in by_id]
        supported = len(sources) == len(block.citation_ids) and _supported(block.text, sources)
        checks.append(CheckResult(
            id=block.block_id, kind="block_support", support_status="supported" if supported else "not_supported",
            reason=None if supported else "insufficient_evidence",
        ))
    cited_documents = {by_id[citation].document_id for block in draft.blocks for citation in block.citation_ids if citation in by_id}
    failed = {
        "global.task_scope": tagged(question, "irrelevant"),
        "global.internal_consistency": tagged(question, "inconsistent"),
        "global.counterevidence": tagged(question, "conflict") and len(cited_documents) < 2,
    }
    for key in GLOBAL_IDS:
        checks.append(CheckResult(
            id=key, kind="global", check_status="fail" if failed[key] else "pass",
            reason=("conflicting_evidence" if key == "global.counterevidence" else "not_provided") if failed[key] else None,
        ))
    return VerificationResult(
        checks=checks, answer_hash=draft.content_hash, evidence_hash=evidence.content_hash, round_id=round_id,
        raw={"fixture_only": True, "mock_version": MOCK_VERSION, "score_semantics": "deterministic_fixture_labels", "quality_measured": False},
    )
