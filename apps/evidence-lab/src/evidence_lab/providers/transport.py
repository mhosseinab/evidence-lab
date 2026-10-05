"""Bounded HTTP attempts and persistent spend reservations.

The transport never logs request bodies, endpoints, headers or provider errors.
All error strings are application-owned. No retries switch providers or models.
"""
from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, TypeVar

import httpx

from evidence_lab.config import input_token_bound
from evidence_lab.domain import CallContext, ProviderError, strict_json

T = TypeVar("T")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def output_reservation(profile: Any) -> int:
    return int(profile.max_output_tokens or 0) if profile.protocol == "chat_completions" else 0


def payload_limit(profile: Any, *, embedding_batch: bool = False) -> int:
    context = profile.effective_max_batch_input_tokens if embedding_batch else profile.max_input_tokens
    if context is None:
        return 0
    return math.floor(context * (1 - profile.context_headroom_fraction)) - output_reservation(profile)


def check_payload(profile: Any, payload: dict, *, embedding_batch: bool = False) -> int:
    try:
        estimate = input_token_bound(payload)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise ProviderError("invalid_response", "Request data cannot be encoded safely") from None
    if estimate > payload_limit(profile, embedding_batch=embedding_batch):
        raise ProviderError("over_budget", "The complete request exceeds the configured input allowance; nothing was truncated")
    if profile.protocol == "cloudflare_clef" and len(payload.get("questions", {})) > profile.max_questions:
        raise ProviderError("over_budget", "The native verifier question limit would be exceeded")
    return estimate


def _usage(data: Any, protocol: str) -> dict[str, int] | None:
    if not isinstance(data, dict):
        return None
    if protocol == "cloudflare_clef":
        data = data.get("result")
        if not isinstance(data, dict):
            return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    known_fields = {"prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens"}
    if any(type(usage[key]) is not int or usage[key] < 0 for key in known_fields & set(usage)):
        return None
    for first, second in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        if first in usage and second in usage and usage[first] != usage[second]:
            return None
    result: dict[str, int] = {}
    input_value = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_value = usage.get("completion_tokens", usage.get("output_tokens"))
    if type(input_value) is int and input_value >= 0:
        result["input_tokens"] = input_value
    if protocol == "embeddings":
        result["output_tokens"] = 0
    elif type(output_value) is int and output_value >= 0:
        result["output_tokens"] = output_value
    total = usage.get("total_tokens")
    if type(total) is int and total >= 0:
        result["total_tokens"] = total
    if {"input_tokens", "output_tokens", "total_tokens"} <= set(result):
        if result["input_tokens"] + result["output_tokens"] != result["total_tokens"]:
            # Contradictory accounting cannot justify reducing a reservation.
            return None
    return result or None


def _cost(profile: Any, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if profile.pricing is None or input_tokens is None or output_tokens is None:
        return None
    result = (input_tokens * profile.pricing.input_usd_per_million + output_tokens * profile.pricing.output_usd_per_million) / 1_000_000
    return result if math.isfinite(result) and result >= 0 else None


def _retry_delay(value: str | None, attempt: int) -> float:
    delay = min(0.1 * 2 ** attempt, 1.0)
    if value:
        try:
            requested = float(value)
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                requested = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                requested = 0.0
        if math.isfinite(requested):
            delay = max(delay, min(max(requested, 0.0), 2.0))
    return delay


class CallExecutor:
    def __init__(self, config: Any, store: Any = None, client: httpx.AsyncClient | httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.store = store
        self._own_client = not isinstance(client, httpx.AsyncClient)
        if isinstance(client, httpx.AsyncBaseTransport):
            self.client: httpx.AsyncClient | None = httpx.AsyncClient(transport=client, follow_redirects=False, trust_env=False)
        else:
            self.client = client
        self._global = asyncio.Semaphore(config.runtime.remote_concurrency)
        self._profiles = {name: asyncio.Semaphore(profile.concurrency) for name, profile in config.profiles.items()}

    async def aclose(self) -> None:
        if self._own_client and self.client is not None:
            await self.client.aclose()

    def _live_ready(self, profile: Any, ctx: CallContext) -> None:
        budgets = self.config.budgets
        if self.store is None:
            raise ProviderError("budget_exhausted", "Live requests require a persistent call-budget store")
        if profile.api_key is None or not profile.api_key.get_secret_value().strip() or not profile.endpoint or not profile.model:
            raise ProviderError("provider_unavailable", "The active provider profile is incomplete")
        if profile.pricing is None:
            raise ProviderError("budget_exhausted", "Live requests require configured dated pricing")
        if budgets.total_max_estimated_cost_usd <= 0 or budgets.phase_max_estimated_cost_usd.get(ctx.phase, 0) <= 0:
            raise ProviderError("budget_exhausted", "Live requests require a positive total and phase budget")

    @asynccontextmanager
    async def _slots(self, name: str, ctx: CallContext):
        acquired_global = acquired_profile = False
        try:
            async with asyncio.timeout(ctx.remaining()):
                await self._global.acquire()
                acquired_global = True
                await self._profiles[name].acquire()
                acquired_profile = True
            ctx.remaining()
            yield
        except TimeoutError:
            raise ProviderError("timeout", "The run deadline expired while waiting for a provider slot") from None
        finally:
            if acquired_profile:
                self._profiles[name].release()
            if acquired_global:
                self._global.release()

    async def _reserve(self, name: str, profile: Any, estimated_cost: float, ctx: CallContext, *, mock: bool) -> str | None:
        if self.store is None:
            ctx.consume_attempt()
            return None
        while True:
            ctx.remaining()
            if ctx.attempts_used >= ctx.max_attempts:
                raise ProviderError("budget_exhausted", "Run attempt limit reached")
            limits = {
                "total_cap": self.config.budgets.total_max_estimated_cost_usd,
                "phase_caps": dict(self.config.budgets.phase_max_estimated_cost_usd),
                "run_attempt_cap": ctx.max_attempts,
                "remote_concurrency": self.config.runtime.remote_concurrency,
                "profile_concurrency": profile.concurrency,
                "mock": mock,
                # The ledger lease starts inside its transaction, before the
                # reservation round trip completes. Reserve through the whole
                # run deadline so DB latency cannot expire a slot while the
                # subsequent bounded HTTP call is still legitimately active.
                "timeout_seconds": ctx.remaining(),
            }
            try:
                call_id = await asyncio.to_thread(self.store.reserve_call, ctx.run_id, ctx.phase, name, estimated_cost, limits)
                # The durable reservation is the attempt boundary. Waiting for
                # another process to free a slot does not consume an attempt.
                ctx.attempts_used += 1
                return call_id
            except ProviderError as exc:
                if exc.status != "concurrency_limited":
                    raise
                await asyncio.sleep(min(0.05, ctx.remaining()))
            except Exception:
                raise ProviderError("provider_unavailable", "Call-budget reservation is unavailable") from None

    async def _finish(self, call_id: str | None, status: str, usage: dict | None, actual_cost: float | None, detail: dict) -> None:
        if call_id is None or self.store is None:
            return
        try:
            await asyncio.to_thread(self.store.finish_call, call_id, status, usage=usage, actual_cost=actual_cost, detail=detail)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("provider_unavailable", "Call-budget completion could not be recorded") from None

    async def _http(self, profile: Any, payload: dict, timeout: float) -> tuple[Any, str | None]:
        if self.client is None:
            self.client = httpx.AsyncClient(follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=self.config.runtime.remote_concurrency))
        secret = profile.api_key.get_secret_value()
        authentication = f"{profile.auth_prefix} {secret}" if profile.auth_prefix else secret
        headers = {profile.auth_header: authentication, "Content-Type": "application/json", "Accept": "application/json"}
        async with self.client.stream("POST", profile.endpoint, headers=headers, json=payload, timeout=timeout, follow_redirects=False) as response:
            retry_after = response.headers.get("Retry-After")
            if response.status_code != 200:
                if response.status_code in {401, 403}:
                    raise ProviderError("provider_unavailable", "Provider authentication or access was rejected")
                error = ProviderError("provider_unavailable", "The configured provider returned an unsuccessful response", retryable=response.status_code in RETRY_STATUS)
                error.retry_after = retry_after
                raise error
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderError("invalid_response", "Provider response exceeded the bounded response size")
            try:
                return strict_json(body.decode("utf-8")), retry_after
            except (ValueError, UnicodeError, TypeError, RecursionError):
                raise ProviderError("invalid_response", "Provider returned an invalid JSON response") from None

    async def invoke(self, name: str, profile: Any, payload: dict, ctx: CallContext, decoder: Callable[[Any], T], *, format_retry: bool = False, embedding_batch: bool = False) -> T:
        ctx.remaining()
        estimate_tokens = check_payload(profile, payload, embedding_batch=embedding_batch)
        self._live_ready(profile, ctx)
        estimated_cost = _cost(profile, estimate_tokens, output_reservation(profile))
        if estimated_cost is None:
            raise ProviderError("budget_exhausted", "A conservative call-cost estimate is unavailable")
        format_retries = 0
        for attempt in range(profile.max_attempts):
            retry_after = None
            failure: ProviderError | None = None
            async with self._slots(name, ctx):
                call_id = await self._reserve(name, profile, estimated_cost, ctx, mock=False)
                started = time.monotonic()
                status = "provider_unavailable"
                usage = None
                actual_cost = None
                try:
                    timeout = min(profile.timeout_seconds, ctx.remaining())
                    async with asyncio.timeout(timeout):
                        data, retry_after = await self._http(profile, payload, timeout)
                        usage = _usage(data, profile.protocol)
                        if usage is not None:
                            actual_cost = _cost(profile, usage.get("input_tokens"), usage.get("output_tokens"))
                        decoded = decoder(data)
                        ctx.remaining()  # A late response can never be published.
                    status = "ok"
                except asyncio.CancelledError:
                    ctx.cancelled = True
                    status = "cancelled"
                    raise
                except (TimeoutError, httpx.TimeoutException):
                    failure = ProviderError("timeout", "The configured provider call timed out", retryable=True)
                    status = failure.status
                except httpx.TransportError:
                    failure = ProviderError("provider_unavailable", "The configured provider could not be reached", retryable=True)
                    status = failure.status
                except ProviderError as exc:
                    failure = exc
                    status = exc.status
                    retry_after = getattr(exc, "retry_after", retry_after)
                except Exception:
                    failure = ProviderError("invalid_response", "Provider processing failed its configured contract")
                    status = failure.status
                finally:
                    detail = {"profile": name, "protocol": profile.protocol, "mode": "live", "attempt": ctx.attempts_used, "latency_seconds": time.monotonic() - started, "estimated_input_tokens": estimate_tokens, "counting_method": "conservative_utf8_bytes", "cost_basis": "reported_usage_at_configured_token_rates" if actual_cost is not None else "reservation_estimate_only"}
                    record = {"call_id": call_id, "phase": ctx.phase, "status": status, "usage": usage, "estimated_cost_usd": estimated_cost, "actual_cost_usd": actual_cost, **detail}
                    ctx.calls.append(record)
                    await self._finish(call_id, status, usage, actual_cost, detail)
            if failure is None:
                ctx.remaining()
                return decoded
            can_retry = failure.retryable
            if failure.status == "invalid_response":
                can_retry = format_retry and failure.retryable and format_retries < 1
                if can_retry:
                    format_retries += 1
            if not can_retry or attempt + 1 >= profile.max_attempts:
                raise failure from None
            delay = _retry_delay(retry_after, attempt)
            if ctx.remaining() <= delay:
                raise ProviderError("timeout", "The run deadline does not permit another provider attempt")
            await asyncio.sleep(delay)
        raise ProviderError("provider_unavailable", "Provider attempts were exhausted")

    async def invoke_mock(self, name: str, profile: Any, payload: dict, ctx: CallContext, fixture: Callable[[], T], *, embedding_batch: bool = False) -> T:
        estimate_tokens = check_payload(profile, payload, embedding_batch=embedding_batch)
        async with self._slots(name, ctx):
            call_id = await self._reserve(name, profile, 0.0, ctx, mock=True)
            started = time.monotonic()
            status = "ok"
            try:
                result = fixture()
                ctx.remaining()
            except asyncio.CancelledError:
                ctx.cancelled = True
                status = "cancelled"
                raise
            except ProviderError as exc:
                status = exc.status
                raise
            except Exception:
                status = "invalid_response"
                raise ProviderError(status, "The deterministic fixture failed its contract") from None
            finally:
                detail = {"profile": name, "protocol": profile.protocol, "mode": "mock", "fixture_only": True, "attempt": ctx.attempts_used, "latency_seconds": time.monotonic() - started, "estimated_input_tokens": estimate_tokens, "counting_method": "conservative_utf8_bytes", "cost_basis": "fixture_no_inference"}
                ctx.calls.append({"call_id": call_id, "phase": ctx.phase, "status": status, "usage": None, "estimated_cost_usd": 0.0, "actual_cost_usd": 0.0, **detail})
                await self._finish(call_id, status, None, 0.0, detail)
            ctx.remaining()
            return result
