"""Scoped tools whose evidence and ledger context are bound by the workflow."""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field

from evidence_lab.domain import CallContext, Draft, EvidencePack, ProviderError


class PrivateStructuredTool(StructuredTool):
    """Disable ambient tracing; graph telemetry uses allowlisted metadata."""

    async def ainvoke(self, input, config=None, **kwargs):
        with tracing_context(enabled=False):
            return await super().ainvoke(input, config=config, **kwargs)


class RetrievalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1)


class SourcePreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_id: str = Field(min_length=1)


def retrieval_tool(*, corpus_id, store, hub, config, context, retrieve):
    """Retrieve a frozen pack only within the current query's corpus."""
    async def retrieve_scoped(question: str):
        return await retrieve(question, corpus_id, store, hub, config, context)

    return PrivateStructuredTool.from_function(
        coroutine=retrieve_scoped,
        name="retrieve_corpus_evidence",
        description="Retrieve supporting document excerpts from the current corpus.",
        args_schema=RetrievalInput,
    )


def source_preview_tool(evidence: EvidencePack):
    """Preview only immutable excerpts included in this query's frozen pack."""
    snapshot = evidence.model_copy(deep=True)

    async def preview(evidence_id: str):
        for item in snapshot.items:
            if item.id == evidence_id:
                return item.model_dump(mode="json")
        raise ProviderError("invalid_response", "The source is outside the current evidence pack")

    return PrivateStructuredTool.from_function(
        coroutine=preview,
        name="preview_evidence_source",
        description="Read an immutable source excerpt by its retrieved evidence ID.",
        args_schema=SourcePreviewInput,
    )


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, pattern=r"\S")
    draft: Draft


def verification_tool(hub, evidence: EvidencePack, context: CallContext, round_id: str = "initial"):
    """Expose native Clef or the configured verifier without replacing its protocol.

    Tool arguments cannot supply evidence, profiles, budgets or verification round.
    Standard ToolCall invocation returns a summary and the full typed artifact;
    only the application policy decides whether an answer can be released.
    """
    snapshot = evidence.model_copy(deep=True)

    async def verify(question: str, draft: Draft):
        result = await hub.verify(question, draft.model_copy(deep=True), snapshot, context, round_id=round_id)
        return result.model_dump(mode="json", exclude={"raw"}), result

    return PrivateStructuredTool.from_function(
        coroutine=verify,
        name="verify_frozen_evidence",
        description="Verify an exact cited draft against the current frozen evidence pack. "
                    "Returns verification checks, not permission to release an answer.",
        args_schema=VerificationInput,
        response_format="content_and_artifact",
    )
