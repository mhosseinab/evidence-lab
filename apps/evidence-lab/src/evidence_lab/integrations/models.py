"""LangChain interfaces that keep transport, budgets and retries in ProviderHub.

These adapters deliberately support asynchronous execution only. Evidence and
call context are bound by the workflow, never supplied by model/tool arguments.
"""
from __future__ import annotations

from typing import Any

from langchain_core.caches import BaseCache
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langsmith import tracing_context
from pydantic import Field

from evidence_lab.domain import CallContext, Draft, EvidencePack, ProviderError


class PrivateRunnable(RunnableLambda):
    """Keep ambient LangSmith environment settings out of provider contents."""

    async def ainvoke(self, input, config=None, **kwargs):
        with tracing_context(enabled=False):
            return await super().ainvoke(input, config=config, **kwargs)


class LedgerChatModel(BaseChatModel):
    """A structured draft chat model backed by the configured ledger provider."""

    hub: Any = Field(exclude=True, repr=False)
    evidence: EvidencePack = Field(exclude=True, repr=False)
    context: CallContext = Field(exclude=True, repr=False)
    repair: dict | None = Field(default=None, exclude=True, repr=False)
    cache: BaseCache | bool | None = False

    @property
    def _llm_type(self) -> str:
        return "evidence-lab-ledger-chat"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("Use ainvoke: provider calls require the asynchronous ledger context")

    async def ainvoke(self, input, config=None, *, stop=None, **kwargs):
        with tracing_context(enabled=False):
            return await super().ainvoke(input, config=config, stop=stop, **kwargs)

    async def _agenerate(
        self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs
    ) -> ChatResult:
        if stop or kwargs:
            raise ProviderError("invalid_response", "Generation options must come from the provider configuration")
        if len(messages) != 1 or not isinstance(messages[0], HumanMessage):
            raise ProviderError("invalid_response", "Draft generation requires one text question")
        question = messages[0].content
        if not isinstance(question, str) or not question.strip():
            raise ProviderError("invalid_response", "Draft generation requires a nonempty text question")
        draft = await self.hub.generate(question, self.evidence, self.context, repair=self.repair)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=draft.model_dump_json()))])

    def with_structured_output(self, schema, *, include_raw=False, **kwargs):
        """Expose the enforced domain schema without a second provider request."""
        if schema is not Draft or include_raw or kwargs:
            raise ValueError("This provider enforces the Draft schema; request with_structured_output(Draft)")
        async def generate(input):
            message = await self.ainvoke(input)
            content = message.content
            if not isinstance(content, str):
                raise ProviderError("invalid_response", "Structured draft generation requires a JSON text response")
            return Draft.model_validate_json(content)

        return PrivateRunnable(generate, name="generate_structured_draft")


class LedgerEmbeddings(Embeddings):
    """Reusable asynchronous embeddings with one shared provider call context."""

    def __init__(self, hub, context: CallContext):
        self.hub = hub
        self.context = context

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Use aembed_documents with the asynchronous ledger context")

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("Use aembed_query with the asynchronous ledger context")

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.hub.embed(texts, self.context)

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.aembed_documents([text]))[0]


def verifier_runnable(hub, evidence: EvidencePack, context: CallContext, round_id: str = "initial"):
    """Keep native Clef checks as structured verification rather than chat text."""
    async def verify(request: dict):
        return await hub.verify(request["question"], request["draft"], evidence, context, round_id=round_id)

    return PrivateRunnable(verify, name="verify_frozen_evidence")
