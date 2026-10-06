"""Real LangChain/OpenAI SDK calls against offline, ledger-observing transports."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import pytest
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langsmith import get_tracing_context

from evidence_lab.config import input_token_bound
from evidence_lab.domain import CallContext, Draft, ProviderError
from evidence_lab.providers import ProviderHub
from evidence_lab.storage import Store
from test_providers import SECRET, Ledger, chat_result, clef_result, configuration, context, draft, evidence


async def test_content_capture_retains_schema_rejected_response_without_ledger_content():
    config = configuration()
    config.langsmith.enabled = True
    config.langsmith.capture_content = True
    ledger = Ledger()
    ctx = context()
    rejected = draft().model_dump(mode="json")
    rejected["blocks"][0]["unexpected"] = "diagnostic response " + SECRET
    response = chat_result(rejected, usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})
    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    try:
        with pytest.raises(ProviderError, match="schema validation"):
            await hub.generate("Private debugging question", evidence(), ctx)
        captured = ctx.provider_traces[0]
        assert captured["status"] == "invalid_response"
        assert "Private debugging question" in json.dumps(captured["content"]["request"])
        assert "diagnostic response" in json.dumps(captured["content"]["response"])
        assert SECRET not in json.dumps(captured, default=str)
        assert "diagnostic response" not in json.dumps(ctx.calls, default=str)
        assert "diagnostic response" not in json.dumps(ledger.rows, default=str)
    finally:
        await hub.aclose()


async def test_live_chat_uses_real_sdk_with_exact_operation_auth_and_private_tracing(monkeypatch):
    config = configuration()
    profile = config.role_profile("generator")
    profile.endpoint = "https://unit.test/custom/generate?api-version=fixture"
    profile.auth_header = "X-Provider-Key"
    profile.auth_prefix = ""
    ledger = Ledger()
    requests: list[httpx.Request] = []
    models: list[ChatOpenAI] = []
    original = ChatOpenAI.ainvoke

    async def observed(model: ChatOpenAI, *args: Any, **kwargs: Any):
        models.append(model)
        assert get_tracing_context()["enabled"] is False
        assert model.max_retries == 0
        assert model.streaming is False
        return await original(model, *args, **kwargs)

    def handler(request: httpx.Request):
        requests.append(request)
        assert ledger.rows[-1]["status"] == "reserved"
        assert str(request.url) == profile.endpoint
        assert request.headers["X-Provider-Key"] == SECRET
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == profile.model
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(200, json=chat_result(draft().model_dump(mode="json"),
            usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}))

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(ChatOpenAI, "ainvoke", observed)
    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(handler))
    try:
        result = await hub.generate("Price?", evidence(), context())
        assert result == draft()
        assert len(models) == len(requests) == len(ledger.rows) == 1
        assert ledger.rows[0]["actual_cost"] == pytest.approx(0.000028)
    finally:
        await hub.aclose()


async def test_embeddings_use_real_sdk_one_reserved_request_per_application_batch(monkeypatch):
    config = configuration()
    profile = config.role_profile("embeddings")
    profile.batch_size = 2
    profile.request_dimensions = True
    profile.endpoint = "https://unit.test/custom/vectors?version=fixture"
    profile.auth_header = "X-Provider-Key"
    profile.auth_prefix = "Token"
    ledger = Ledger()
    batches: list[list[str]] = []
    original = OpenAIEmbeddings.aembed_documents
    calls: list[OpenAIEmbeddings] = []

    async def observed(model: OpenAIEmbeddings, texts: list[str], *args: Any, **kwargs: Any):
        calls.append(model)
        assert get_tracing_context()["enabled"] is False
        assert model.max_retries == 0
        assert model.check_embedding_ctx_length is False
        assert model.chunk_size >= len(texts)
        return await original(model, texts, *args, **kwargs)

    def handler(request: httpx.Request):
        assert ledger.rows[-1]["status"] == "reserved"
        assert str(request.url) == profile.endpoint
        assert request.headers["X-Provider-Key"] == f"Token {SECRET}"
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body["encoding_format"] == "float"
        assert body["dimensions"] == 3
        batches.append(body["input"])
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [1, 0, 0]} for index in range(len(body["input"]))
        ], "usage": {"prompt_tokens": 2, "total_tokens": 2}})

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(OpenAIEmbeddings, "aembed_documents", observed)
    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(handler))
    try:
        texts = ["one", "two", "three", "four", "five"]
        assert await hub.embed(texts, context()) == [[1.0, 0.0, 0.0]] * 5
        assert batches == [["one", "two"], ["three", "four"], ["five"]]
        assert len(calls) == len(ledger.rows) == 3
        assert all(row["status"] == "ok" for row in ledger.rows)
    finally:
        await hub.aclose()


async def test_sdk_retry_is_owned_by_executor_and_gets_another_reservation():
    config = configuration()
    ledger = Ledger()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        assert len(ledger.rows) == len(requests)
        assert ledger.rows[-1]["status"] == "reserved"
        if len(requests) == 1:
            return httpx.Response(429, json={"error": {"message": "private-provider-error"}})
        return httpx.Response(200, json=chat_result(draft().model_dump(mode="json")))

    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(handler))
    try:
        assert await hub.generate("Price?", evidence(), context()) == draft()
        assert [row["status"] for row in ledger.rows] == ["provider_unavailable", "ok"]
        assert len(requests) == 2
    finally:
        await hub.aclose()


async def test_retry_cap_prevents_sdk_from_sending_unreserved_request():
    config = configuration()
    ledger = Ledger()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        # Exhaust the durable budget after the first ambiguous provider result.
        config.budgets.total_max_estimated_cost_usd = ledger.rows[0]["estimated_cost"]
        return httpx.Response(429, json={"error": {"message": "do not expose"}})

    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as failure:
            await hub.generate("Price?", evidence(), context())
        assert failure.value.status == "budget_exhausted"
        assert len(requests) == len(ledger.rows) == 1
        assert ledger.rows[0]["actual_cost"] is None
    finally:
        await hub.aclose()


async def test_invalid_raw_usage_is_not_reconciled_from_sdk_coercions():
    ledger = Ledger()
    hub = ProviderHub(configuration(), store=ledger, client=httpx.MockTransport(lambda _request:
        httpx.Response(200, json=chat_result(draft().model_dump(mode="json"),
            usage={"prompt_tokens": "12", "completion_tokens": 8, "total_tokens": 20}))))
    try:
        assert await hub.generate("Price?", evidence(), context()) == draft()
        assert ledger.rows[0]["usage"] is None
        assert ledger.rows[0]["actual_cost"] is None
        assert ledger.rows[0]["estimated_cost"] > 0
    finally:
        await hub.aclose()


async def test_additional_sdk_request_is_rejected_before_underlying_transport(monkeypatch):
    config = configuration()
    ledger = Ledger()
    requests: list[httpx.Request] = []
    original = ChatOpenAI.ainvoke

    async def extra_request(model: ChatOpenAI, *args: Any, **kwargs: Any):
        result = await original(model, *args, **kwargs)
        # Simulate unexpected SDK behavior after its admitted request succeeded.
        await model.root_async_client.chat.completions.create(
            model=model.model_name, messages=[{"role": "user", "content": "extra request"}])
        return result

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json=chat_result(draft().model_dump(mode="json")))

    monkeypatch.setattr(ChatOpenAI, "ainvoke", extra_request)
    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError):
            await hub.generate("Price?", evidence(), context())
        assert len(requests) == len(ledger.rows) == 1
    finally:
        await hub.aclose()


async def test_native_clef_keeps_its_transport_and_never_uses_openai_sdk(monkeypatch):
    config = configuration(native=True)
    ledger = Ledger()
    requests: list[httpx.Request] = []

    async def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError("Native Clef must not enter an OpenAI SDK integration")

    def handler(request: httpx.Request):
        requests.append(request)
        assert str(request.url) == config.role_profile("verifier").endpoint
        assert "questions" in json.loads(request.content)
        return httpx.Response(200, json=clef_result())

    monkeypatch.setattr(ChatOpenAI, "ainvoke", forbidden)
    monkeypatch.setattr(OpenAIEmbeddings, "aembed_documents", forbidden)
    hub = ProviderHub(config, store=ledger, client=httpx.MockTransport(handler))
    try:
        result = await hub.verify("Price?", draft(), evidence(), context())
        assert result.checks[0].support_status == "supported"
        assert len(requests) == len(ledger.rows) == 1
        assert ledger.rows[0]["detail"]["protocol"] == "cloudflare_clef"
    finally:
        await hub.aclose()


async def test_sdk_payload_mutation_is_rejected_without_reservation_or_network(monkeypatch):
    ledger = Ledger()
    requests: list[httpx.Request] = []
    original = ChatOpenAI.ainvoke

    async def altered_request(model: ChatOpenAI, *args: Any, **kwargs: Any):
        # More completions would exceed the configured output reservation.
        return await original(model, *args, **{**kwargs, "n": 2})

    def handler(request: httpx.Request):
        requests.append(request)
        raise AssertionError("An altered SDK payload must fail before network access")

    monkeypatch.setattr(ChatOpenAI, "ainvoke", altered_request)
    hub = ProviderHub(configuration(), store=ledger, client=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as failure:
            await hub.generate("Price?", evidence(), context())
        assert failure.value.status == "invalid_response"
        assert requests == ledger.rows == []
    finally:
        await hub.aclose()


@pytest.mark.integration
@pytest.mark.native_postgres
async def test_concurrent_sdk_calls_cannot_exceed_persistent_postgres_budget(isolated_storage_dsn):
    if os.environ.get("EVIDENCE_LAB_TEST_BACKEND", "").lower() == "pglite":
        pytest.skip("PGlite does not establish native multi-session budget admission.")
    config = configuration()
    profile = config.role_profile("generator")
    assert profile.pricing is not None
    store = Store(isolated_storage_dsn)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request):
        requests.append(request)
        # Give the competing call time to request its own independent admission.
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=chat_result(draft().model_dump(mode="json")))

    hub = ProviderHub(config, store=store, client=httpx.MockTransport(handler))
    payload = hub._generation_payload(profile, "Price?", evidence(), None)
    estimate = (input_token_bound(payload) * profile.pricing.input_usd_per_million
                + (profile.max_output_tokens or 0) * profile.pricing.output_usd_per_million) / 1_000_000
    cap = estimate * 1.1
    config.budgets.total_max_estimated_cost_usd = cap
    config.budgets.phase_max_estimated_cost_usd["queries"] = cap
    try:
        results = await asyncio.gather(*[
            hub.generate("Price?", evidence(), CallContext.for_seconds(f"sdk-budget-{index}", "queries", 20, 10))
            for index in range(2)
        ], return_exceptions=True)
        completed = [result for result in results if isinstance(result, Draft)]
        failures = [result for result in results if isinstance(result, ProviderError)]
        assert completed == [draft()]
        assert len(failures) == 1 and failures[0].status == "budget_exhausted"
        assert len(requests) == 1
        calls = store.get_calls()
        assert len(calls) == 1
        assert calls[0]["status"] == "ok"
        assert calls[0]["charged_cost"] == pytest.approx(estimate)
        assert calls[0]["charged_cost"] < cap
    finally:
        await hub.aclose()
