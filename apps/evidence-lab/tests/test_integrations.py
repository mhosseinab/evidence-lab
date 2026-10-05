"""Standard integrations preserve trusted scope and ledger execution offline."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langsmith import get_tracing_context
from pydantic import ValidationError

from evidence_lab.config import load_config
from evidence_lab.domain import CallContext, Draft, EvidenceItem, EvidencePack, ProviderError
from evidence_lab.integrations.models import LedgerChatModel, LedgerEmbeddings
from evidence_lab.integrations.tools import retrieval_tool, source_preview_tool, verification_tool
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
        assert isinstance(message.content, str)
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
        tool = verification_tool(hub, pack, context, "repair")
        message = await tool.ainvoke({"type": "tool_call", "id": "verify-repair", "name": tool.name,
                                    "args": {"question": "What does the widget cost?",
                                             "draft": draft.model_dump(mode="json")}})
        result = message.artifact
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


async def test_structured_chain_forwards_runnable_callbacks_without_extra_calls(pack, context):
    tags_seen = []

    class Observer(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, *, tags=None, **kwargs):
            tags_seen.append(tags)

    hub = ProviderHub(load_config(ROOT / "configs/mock.yaml"))
    try:
        model = LedgerChatModel(hub=hub, evidence=pack, context=context)
        result = await model.with_structured_output(Draft).ainvoke(
            "What does the widget cost?",
            config={"tags": ["provider-audit"], "callbacks": [Observer()]},
        )
        assert isinstance(result, Draft)
        assert tags_seen and "provider-audit" in tags_seen[0]
        assert context.attempts_used == 1
    finally:
        await hub.aclose()


async def test_native_clef_tool_returns_summary_and_typed_artifact(monkeypatch):
    import json

    import httpx
    from langchain_core.messages import ToolMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from test_providers import Ledger, clef_result, configuration, context, draft, evidence

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    ledger = Ledger()
    pack = evidence()
    frozen_hash = pack.content_hash
    original_text = pack.items[0].text
    answer = draft()
    ctx = context()
    calls, callbacks = [], []

    class Observer(BaseCallbackHandler):
        def on_tool_start(self, serialized, input_str, *, tags=None, **kwargs):
            callbacks.append(tags)

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert "questions" in body and "messages" not in body
        assert body["state"]["evidence"]["items"][0]["text"] == original_text
        assert ledger.rows[0]["status"] == "reserved"
        assert get_tracing_context()["enabled"] is False
        return httpx.Response(200, json=clef_result())

    config = configuration(native=True)
    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(handler))
    try:
        tool = verification_tool(hub, pack, ctx, "repair")
        schema = convert_to_openai_tool(tool)
        assert set(schema["function"]["parameters"]["properties"]) == {"question", "draft"}
        pack.items[0].text = "Changed after the tool was bound"
        message = await tool.ainvoke(
            {"type": "tool_call", "id": "clef-1", "name": tool.name,
             "args": {"question": "Price?", "draft": answer.model_dump(mode="json")}},
            config={"tags": ["clef-tool"], "callbacks": [Observer()]},
        )
        assert isinstance(message, ToolMessage)
        assert isinstance(message.content, str)
        summary = json.loads(message.content)
        result = message.artifact
        assert result.answer_hash == answer.content_hash and result.evidence_hash == frozen_hash
        assert result.round_id == summary["round_id"] == "repair"
        assert result.raw["schema"] == "cloudflare-clef-choice-v1"
        assert "raw" not in summary and "native_answers" not in message.content
        assert callbacks and "clef-tool" in callbacks[0]
        assert len(calls) == len(ledger.rows) == ctx.attempts_used == 1
        assert str(calls[0].url) == config.role_profile("verifier").endpoint
    finally:
        await hub.aclose()


@pytest.mark.parametrize("extra", ["evidence", "context", "round_id", "profile", "budget"])
async def test_verification_tool_rejects_scope_overrides_before_spending(pack, context, extra):
    calls = []

    async def verify(*args, **kwargs):
        calls.append(1)
        raise AssertionError("Invalid tool input must fail before verification")

    tool = verification_tool(SimpleNamespace(verify=verify), pack, context)
    answer = Draft.model_validate({"blocks": [{"block_id": "b1", "text": "seven dollars",
                                             "citation_ids": ["e1"]}]})
    with pytest.raises(ValidationError):
        await tool.ainvoke({"question": "Price?", "draft": answer.model_dump(mode="json"), extra: {}})
    for bad_question in ("", "   ", 123):
        with pytest.raises(ValidationError):
            await tool.ainvoke({"question": bad_question, "draft": answer.model_dump(mode="json")})
    with pytest.raises(ValidationError):
        await tool.ainvoke({"question": "Price?", "draft": {"blocks": []}})
    assert calls == [] and context.attempts_used == 0


async def test_verification_tool_propagates_budget_failure_without_retry(pack, context):
    calls = []

    async def verify(*args, **kwargs):
        calls.append(1)
        raise ProviderError("budget_exhausted", "Budget exhausted")

    tool = verification_tool(SimpleNamespace(verify=verify), pack, context)
    answer = Draft.model_validate({"blocks": [{"block_id": "b1", "text": "seven dollars",
                                             "citation_ids": ["e1"]}]})
    with pytest.raises(ProviderError, match="Budget exhausted"):
        await tool.ainvoke({"question": "Price?", "draft": answer.model_dump(mode="json")})
    assert calls == [1]
