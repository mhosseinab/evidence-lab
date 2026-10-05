"""Explicit, bounded LangSmith export of workflow metadata only."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from langsmith import Client
from urllib3.util.retry import Retry

from evidence_lab.graph import NODES

LOGGER = logging.getLogger(__name__)


def trace_records(config, run_id: str, steps: list[dict], result: dict) -> list[dict]:
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
    order = start.strftime("%Y%m%dT%H%M%S%fZ") + str(trace_id)
    status = result.get("status")
    allowed_statuses = {"answered", "shadow", "abstained", "timed_out", "cancelled", "failed", "verification_unavailable"}
    status = status if status in allowed_statuses else "failed"
    parent = {"id": trace_id, "trace_id": trace_id, "dotted_order": order,
              "name": "Evidence Lab query", "run_type": "chain", "session_name": config.langsmith.project,
              "start_time": start, "end_time": end, "inputs": {"application_run_id": run_id},
              "outputs": {"status": status, "node_count": len(safe_steps)},
              "extra": {"metadata": {"orchestration": "langgraph", "content": "metadata_only", "mode": config.runtime.mode}}}
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
    return records


def _send(config, records):
    settings = config.langsmith
    client = Client(api_url=settings.api_url, api_key=settings.api_key.get_secret_value(),
                    workspace_id=settings.workspace_id, timeout_ms=int(settings.timeout_seconds * 1000),
                    retry_config=Retry(total=0), auto_batch_tracing=False, tracing_mode="langsmith",
                    omit_traced_runtime_info=True, disable_prompt_cache=True)
    try:
        client.batch_ingest_runs(create=records)
    finally:
        client.close(timeout=0)


async def export_trace(config, run_id: str, steps: list[dict], result: dict) -> None:
    """Tracing outages cannot change a verified query result or expose errors."""
    if not config.langsmith.enabled:
        return
    try:
        records = trace_records(config, run_id, steps, result)
        await asyncio.wait_for(asyncio.to_thread(_send, config, records), timeout=config.langsmith.timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.warning("LangSmith workflow metadata export was unavailable.")
