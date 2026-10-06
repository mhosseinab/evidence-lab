"""Explicit, bounded LangSmith export with optional redacted provider content."""
from __future__ import annotations

import asyncio
import logging
import json
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from langsmith import Client
from urllib3.util.retry import Retry

from evidence_lab.graph import NODES

LOGGER = logging.getLogger(__name__)
MAX_CONTENT_BYTES = 1024 * 1024


def capture_provider_call(config, ctx, profile_name: str, payload: dict, response, status: str, elapsed: float) -> None:
    """Keep opt-in content in memory, separate from persistent call accounting."""
    if not config.langsmith.enabled or not config.langsmith.capture_content:
        return
    try:
        secrets = [config.runtime.operator_token, config.langsmith.api_key, config.database.dsn]
        secrets.extend(profile.api_key for profile in config.profiles.values())
        values = [value.get_secret_value() if hasattr(value, "get_secret_value") else value for value in secrets if value]

        def redact(value):
            if isinstance(value, dict):
                return {key: "[REDACTED]" if key.lower() in {
                    "authorization", "api_key", "cookie", "set-cookie", "password", "access_token",
                } else redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, str):
                for secret in values:
                    value = value.replace(secret, "[REDACTED]")
            return value

        content = redact({"request": payload, "response": response})
        size = len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
        used = sum(call["content_bytes"] for call in ctx.provider_traces)
        omitted = used + size > MAX_CONTENT_BYTES
        end = datetime.now(timezone.utc)
        ctx.provider_traces.append({"profile": profile_name, "status": status,
                                   "start_time": end - timedelta(seconds=elapsed), "end_time": end,
                                   "content": {} if omitted else content,
                                   "content_bytes": 0 if omitted else size, "content_omitted": omitted})
    except Exception:
        LOGGER.warning("LangSmith provider content capture was unavailable.")


def trace_records(config, run_id: str, steps: list[dict], result: dict, *, provider_calls: list[dict] | None = None,
                  name: str = "Evidence Lab query") -> list[dict]:
    """Allowlist trace fields; never serialize graph state, prompts or sources."""
    safe_steps = []
    for step in steps[:20]:
        if step.get("node") not in NODES or step.get("status") not in {"completed", "failed"}:
            continue
        elapsed = step.get("elapsed_seconds", 0)
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400:
            continue
        safe_steps.append({"node": step["node"], "status": step["status"], "elapsed_seconds": elapsed})
    trace_id = uuid4()
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=sum(step["elapsed_seconds"] for step in safe_steps))
    if config.langsmith.capture_content and provider_calls:
        start = min(start, *(call["start_time"] for call in provider_calls))
    order = start.strftime("%Y%m%dT%H%M%S%fZ") + str(trace_id)
    status = result.get("status")
    allowed_statuses = {"answered", "shadow", "abstained", "timed_out", "cancelled", "failed", "verification_unavailable"}
    status = status if status in allowed_statuses else "failed"
    parent = {"id": trace_id, "trace_id": trace_id, "dotted_order": order,
              "name": name, "run_type": "chain", "session_name": config.langsmith.project,
              "start_time": start, "end_time": end, "inputs": {"application_run_id": run_id},
              "outputs": {"status": status, "node_count": len(safe_steps)},
              "error": "Workflow failed" if status in {"failed", "timed_out", "verification_unavailable"} else None,
              "extra": {"metadata": {"orchestration": "langgraph", "content": "provider_payloads" if config.langsmith.capture_content else "metadata_only", "mode": config.runtime.mode}}}
    records = [parent]
    current = start
    for step in safe_steps:
        child_id = uuid4()
        finished = current + timedelta(seconds=step["elapsed_seconds"])
        records.append({"id": child_id, "trace_id": trace_id, "parent_run_id": trace_id,
                        "dotted_order": order + "." + current.strftime("%Y%m%dT%H%M%S%fZ") + str(child_id),
                        "name": step["node"], "run_type": "chain", "session_name": config.langsmith.project,
                        "start_time": current, "end_time": finished, "inputs": {},
                        "outputs": {"status": step["status"], "elapsed_seconds": step["elapsed_seconds"]},
                        "error": "Workflow stage failed" if step["status"] == "failed" else None})
        current = finished
    if config.langsmith.capture_content:
        for call in provider_calls or []:
            child_id = uuid4()
            records.append({"id": child_id, "trace_id": trace_id, "parent_run_id": trace_id,
                            "dotted_order": order + "." + call["start_time"].strftime("%Y%m%dT%H%M%S%fZ") + str(child_id),
                            "name": call["profile"], "run_type": "llm", "session_name": config.langsmith.project,
                            "start_time": call["start_time"], "end_time": call["end_time"],
                            "inputs": {"request": call["content"].get("request")},
                            "outputs": {"response": call["content"].get("response"), "status": call["status"]},
                            "error": "Provider call failed: " + call["status"] if call["status"] != "ok" else None,
                            "extra": {"metadata": {"content_omitted": call["content_omitted"]}}})
    return records


def _send(config, records):
    settings = config.langsmith
    client = Client(api_url=settings.api_url, api_key=settings.api_key.get_secret_value(),
                    workspace_id=settings.workspace_id, timeout_ms=int(settings.timeout_seconds * 1000),
                    retry_config=Retry(total=0), auto_batch_tracing=False, tracing_mode="langsmith",
                    omit_traced_runtime_info=True, disable_prompt_cache=True, hide_inputs=False, hide_outputs=False)
    try:
        client.batch_ingest_runs(create=records)
    finally:
        client.close(timeout=0)


async def export_trace(config, run_id: str, steps: list[dict], result: dict, *, provider_calls: list[dict] | None = None,
                       name: str = "Evidence Lab query") -> None:
    """Tracing outages cannot change a verified query result or expose errors."""
    if not config.langsmith.enabled:
        return
    try:
        records = trace_records(config, run_id, steps, result, provider_calls=provider_calls, name=name)
        await asyncio.wait_for(asyncio.to_thread(_send, config, records), timeout=config.langsmith.timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.warning("LangSmith workflow export was unavailable.")
