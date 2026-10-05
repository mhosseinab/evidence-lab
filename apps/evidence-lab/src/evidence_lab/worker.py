"""Durable leased workers, bounded task concurrency and fenced publication."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

from evidence_lab.domain import ProviderError
from evidence_lab.engine import QueryEngine
from evidence_lab.ingestion import ingest_job
from evidence_lab.storage import TERMINAL_RUNS

LOGGER = logging.getLogger("evidence_lab.worker")


class Worker:
    def __init__(self, store, hub, config, *, worker_id=None, concurrency=None):
        self.store, self.hub, self.config = store, hub, config
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.concurrency = concurrency or config.runtime.remote_concurrency
        self.stopping = asyncio.Event()
        self.active: set[asyncio.Task] = set()

    async def dispatch(self, job):
        if job["kind"] == "ingest":
            return await ingest_job(job, self.store, self.hub, self.config)
        if job["kind"] == "query":
            existing = self.store.get_run(job["payload"]["run_id"])
            if existing["status"] in TERMINAL_RUNS:
                # Recover a crash after atomic answer publication but before the
                # job acknowledgement; never repeat inference for this answer.
                return {"run_id": existing["id"], "status": existing["status"], "recovered": True}
            return await QueryEngine(self.store, self.hub, self.config).run(job)
        if job["kind"] == "evaluation":
            from evidence_lab.evaluation import evaluate_dataset, run_evaluation
            payload = job["payload"]
            if "operator_dataset_path" in payload:
                if set(payload) != {"operator_dataset_path", "split", "annotations_path", "config_fingerprint"}:
                    raise ProviderError("invalid_job", "Operator evaluation fields are invalid.")
                if payload["config_fingerprint"] != self.config.fingerprint():
                    raise ProviderError("configuration_changed", "Evaluation configuration changed before execution.")
                return await evaluate_dataset(payload["operator_dataset_path"], self.store, self.hub, self.config,
                    job_id=job["id"], split=payload["split"], annotations_path=payload["annotations_path"], worker_job=job)
            return await run_evaluation(job, self.store, self.hub, self.config)
        raise ProviderError("invalid_job", "Unsupported durable job type.")

    async def _heartbeat(self, job, task):
        interval = min(10.0, self.config.runtime.worker_lease_seconds / 3)
        while not task.done():
            await asyncio.sleep(interval)
            try:
                valid = await asyncio.to_thread(self.store.renew_job, job["id"], job["token"],
                                                self.config.runtime.worker_lease_seconds)
            except ProviderError:
                valid = False
            if not valid:
                task.cancel()
                return

    async def process(self, job):
        operation = asyncio.create_task(self.dispatch(job))
        heartbeat = asyncio.create_task(self._heartbeat(job, operation))
        try:
            result = await operation
            status = result.get("status", "succeeded")
            if status in {"needs_ocr", "needs_review"}:
                job_status = status
            elif status in {"failed", "timed_out", "verification_unavailable", "incomplete"}:
                job_status = "failed"
            elif status == "cancelled":
                job_status = "cancelled"
            else:
                job_status = "succeeded"
            self.store.finish_job(job["id"], job["token"], job_status, result=result)
            return result
        except asyncio.CancelledError:
            # A cancelled or expired lease cannot publish even a failure result.
            operation.cancel()
            with suppress(asyncio.CancelledError):
                await operation
            return {"status": "cancelled", "code": "lease_or_shutdown"}
        except Exception as exc:
            code = exc.status if isinstance(exc, ProviderError) else "internal_error"
            message = str(exc) if isinstance(exc, ProviderError) else "The worker operation could not be completed."
            LOGGER.warning("Job %s stopped (%s)", job["id"], code)
            if code != "lease_lost":
                try:
                    if job["kind"] == "query":
                        self.store.update_run(job["payload"]["run_id"],
                                              {"status": "failed", "answer": None, "blocks": [],
                                               "code": code, "error": message}, lease=job)
                    self.store.finish_job(job["id"], job["token"], "failed",
                                          error={"code": code, "detail": message})
                except ProviderError:
                    pass  # Fencing or an already-terminal run is authoritative.
            return {"status": "failed", "code": code}
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def run_once(self):
        job = await asyncio.to_thread(self.store.claim_job, self.worker_id,
                                      self.config.runtime.worker_lease_seconds)
        return await self.process(job) if job else None

    async def run_forever(self):
        try:
            while not self.stopping.is_set():
                self.active = {task for task in self.active if not task.done()}
                if len(self.active) < self.concurrency:
                    try:
                        job = await asyncio.to_thread(self.store.claim_job, self.worker_id,
                                                      self.config.runtime.worker_lease_seconds)
                    except ProviderError:
                        LOGGER.warning("Worker is waiting for database availability.")
                        job = None
                    if job:
                        self.active.add(asyncio.create_task(self.process(job)))
                        continue
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=self.config.runtime.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            for task in self.active:
                task.cancel()
            await asyncio.gather(*self.active, return_exceptions=True)

    def stop(self):
        self.stopping.set()
