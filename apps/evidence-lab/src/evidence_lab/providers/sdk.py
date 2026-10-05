"""OpenAI integrations with a single-use, ledger-authorized HTTP boundary."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

import httpx
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langsmith import tracing_context

from evidence_lab.domain import ProviderError, strict_json

SDK_BASE_URL = "https://sdk.evidence-lab.invalid/v1"
Send = Callable[[dict], Awaitable[tuple[Any, str | None]]]


class RequestGuard(httpx.AsyncBaseTransport):
    """Only an expected SDK request can reach the scoped reservation callback."""

    def __init__(self, profile: Any, payload: dict, send: Send):
        self.profile = profile
        self.payload = payload
        self.send = send
        self.used = False
        self.error: Exception | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            operation = "embeddings" if self.profile.protocol == "embeddings" else "chat/completions"
            if self.used or request.method != "POST" or str(request.url) != f"{SDK_BASE_URL}/{operation}":
                raise ProviderError("invalid_response", "The SDK attempted an unauthorized provider request")
            self.used = True
            payload = strict_json((await request.aread()).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ProviderError("invalid_response", "The SDK request did not contain a JSON object")
            # ChatOpenAI normalizes these fields; retain explicit profile contracts.
            if "max_tokens" in self.payload and "max_completion_tokens" in payload:
                payload["max_tokens"] = payload.pop("max_completion_tokens")
            if re.match(r"^o\d", self.profile.model):
                for actual, expected in zip(payload.get("messages", []), self.payload.get("messages", [])):
                    if actual.get("role") == "developer" and expected.get("role") == "system":
                        actual["role"] = "system"
            if payload != self.payload:
                raise ProviderError("invalid_response", "The SDK request changed its configured provider contract")
            data, retry_after = await self.send(payload)
            headers = {"Retry-After": retry_after} if retry_after else None
            return httpx.Response(200, json=data, headers=headers, request=request)
        except Exception as exc:
            self.error = exc
            raise


async def request_with_sdk(profile: Any, payload: dict, timeout: float, send: Send) -> None:
    """Execute real SDK integrations; the guard validates raw data before SDK parsing.

    SDK credentials/URLs are inert placeholders. Only the reservation callback
    knows the real configured endpoint and authentication. Sync clients are never
    created, and each explicit executor retry receives a new one-shot guard.
    """
    guard = RequestGuard(profile, payload, send)
    async with httpx.AsyncClient(transport=guard, follow_redirects=False, trust_env=False) as client:
        settings: dict[str, Any] = {
            "model": profile.model, "api_key": "ledger-guarded", "base_url": SDK_BASE_URL,
            "max_retries": 0, "http_async_client": client, "client": object(),
            "timeout": timeout, "openai_proxy": "",
        }
        try:
            with tracing_context(enabled=False):
                if profile.protocol == "embeddings":
                    embeddings = OpenAIEmbeddings(
                        **settings, check_embedding_ctx_length=False, tiktoken_enabled=False,
                        chunk_size=len(payload["input"]),
                        dimensions=payload.get("dimensions"),
                        model_kwargs={"encoding_format": "float"},
                    )
                    await embeddings.aembed_documents(payload["input"])
                else:
                    model = ChatOpenAI(
                        **settings, use_responses_api=False, stream_usage=False, cache=False,
                    )
                    options = {key: value for key, value in payload.items() if key not in {"model", "messages", "stream"}}
                    await model.ainvoke(payload["messages"], **options)
        except Exception:
            if guard.error is not None:
                raise guard.error from None
            raise ProviderError("invalid_response", "The OpenAI SDK could not process the validated response") from None
        if not guard.used:
            raise ProviderError("invalid_response", "The OpenAI SDK did not issue its expected request")
