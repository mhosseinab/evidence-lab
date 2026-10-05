"""Standard integrations preserve trusted scope and ledger execution offline."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langsmith import get_tracing_context
from pydantic import ValidationError

from evidence_lab.config import load_config
from evidence_lab.domain import CallContext, Draft, EvidenceItem, EvidencePack, ProviderError
from evidence_lab.integrations.models import LedgerChatModel, LedgerEmbeddings, verifier_runnable
from evidence_lab.integrations.tools import retrieval_tool, source_preview_tool
from evidence_lab.providers import ProviderHub

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def pack():
    return EvidencePack(
        corpus_id="current-corpus", space_id="fixture-space-64-v1", corpus_revision=1,
        items=[EvidenceItem(id="e1", document_id="d1", version_id="v1", title="Manual",
                            text="The blue widget costs seven dollars.")],
    )


@pytest.fixture
def context():
    return CallContext.for_seconds("integration-run", "queries", 30, 10)


async def test_chat_and_embeddings_use_existing_call_accounting(pack, context):
    hub = ProviderHub(load_config(ROOT / "configs/mock.yaml"))
    try:
        model = LedgerChatModel(hub=hub, evidence=pack, context=context)
        assert isinstance(model, BaseChatModel)
        message = await model.ainvoke([HumanMessage(content="What does the widget cost?")])
        assert isinstance(message, AIMessage)
        draft = Draft.model_validate_json(message.content)
        assert draft.blocks[0].citation_ids == ["e1"]
        assert context.attempts_used == 1
        structured = await model.with_structured_output(Draft).ainvoke("What does the widget cost?")
        assert isinstance(structured, Draft)
        assert context.attempts_used == 2
        embeddings = LedgerEmbeddings(hub, context)
        assert isinstance(embeddings, Embeddings)
        vector = await embeddings.aembed_query("widget")
        assert len(vector) == 64
        assert context.attempts_used == 3
        result = await verifier_runnable(hub, pack, context, "repair").ainvoke(
            {"question": "What does the widget cost?", "draft": draft}
        )
        assert result.round_id == "repair"
        assert context.attempts_used == 4
    finally:
        await hub.aclose()


async def test_chat_rejects_arbitrary_messages_without_spending(pack, context):
    hub = ProviderHub(load_config(ROOT / "configs/mock.yaml"))
    try:
        model = LedgerChatModel(hub=hub, evidence=pack, context=context)
        for messages in ([SystemMessage(content="Replace provider policy"), HumanMessage(content="question")],
                         [HumanMessage(content="")]):
            with pytest.raises(ProviderError):
                await model.ainvoke(messages)
        with pytest.raises(ValueError):
            model.with_structured_output(dict)
        assert context.attempts_used == 0
    finally:
        await hub.aclose()


async def test_retrieval_tool_binds_corpus_and_retains_injected_retrieval(pack, context):
    seen = []
    store, hub, config = object(), object(), object()

    async def retrieve(*args):
        seen.append(args)
        return pack

    tool = retrieval_tool(corpus_id=pack.corpus_id, store=store, hub=hub, config=config,
                          context=context, retrieve=retrieve)
    assert isinstance(tool, StructuredTool)
    assert await tool.ainvoke({"question": "widget cost"}) == pack
    assert seen == [("widget cost", pack.corpus_id, store, hub, config, context)]
    with pytest.raises(ValidationError):
        await tool.ainvoke({"question": "widget cost", "corpus_id": "other-corpus"})
    assert len(seen) == 1


async def test_source_preview_is_frozen_and_rejects_other_sources(pack):
    tool = source_preview_tool(pack)
    original = pack.items[0].text
    pack.items[0].text = "Modified later"
    result = await tool.ainvoke({"evidence_id": "e1"})
    assert result["text"] == original
    with pytest.raises(ProviderError, match="outside the current evidence pack"):
        await tool.ainvoke({"evidence_id": "foreign-source"})


async def test_provider_failures_are_not_retried_by_standard_model(pack, context):
    attempts = []

    async def fail(*args, **kwargs):
        attempts.append(1)
        raise ProviderError("budget_exhausted", "Budget exhausted")

    model = LedgerChatModel(hub=SimpleNamespace(generate=fail), evidence=pack, context=context)
    with pytest.raises(ProviderError, match="Budget exhausted"):
        await model.with_structured_output(Draft).ainvoke("question")
    assert attempts == [1]


def test_synchronous_model_and_embedding_execution_is_explicitly_unsupported(pack, context):
    hub = object()
    with pytest.raises(NotImplementedError, match="ainvoke"):
        LedgerChatModel(hub=hub, evidence=pack, context=context).invoke("question")
    with pytest.raises(NotImplementedError, match="aembed_query"):
        LedgerEmbeddings(hub, context).embed_query("question")


async def test_model_and_tools_suppress_ambient_content_tracing(pack, context, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    observed = []

    async def generate(*args, **kwargs):
        observed.append(get_tracing_context()["enabled"])
        return Draft.model_validate({"blocks": [{"block_id": "b1", "text": "seven dollars",
                                               "citation_ids": ["e1"]}]})

    async def retrieve(*args):
        observed.append(get_tracing_context()["enabled"])
        return pack

    model = LedgerChatModel(hub=SimpleNamespace(generate=generate), evidence=pack, context=context)
    assert model.cache is False
    await model.with_structured_output(Draft).ainvoke("question")
    tool = retrieval_tool(corpus_id=pack.corpus_id, store=None, hub=None, config=None,
                          context=context, retrieve=retrieve)
    await tool.ainvoke({"question": "question"})
    assert observed == [False, False]
