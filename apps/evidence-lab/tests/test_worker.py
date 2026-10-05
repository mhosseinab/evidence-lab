"""Focused durable-worker recovery and publication-boundary tests."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from evidence_lab import worker as worker_module
from evidence_lab.config import load_config
from evidence_lab.domain import ProviderError
from evidence_lab.storage import TERMINAL_RUNS
from evidence_lab.worker import Worker


class WorkerStore:
    def __init__(self, *, kind="query", run_status="queued", fail_first_ack=False):
        self.job = {"id": "job-fixture", "kind": kind, "payload": {"run_id": "run-fixture"} if kind == "query" else {"version_id": "v1"}, "status": "queued", "token": None}
        self.run = {"id": "run-fixture", "status": run_status, "answer": "Previously checked fixture answer" if run_status == "answered" else None, "blocks": []}
        self.claim_count = 0
        self.acknowledgments = []
        self.publications = []
        self.fail_first_ack = fail_first_ack
        self.renewals = 0
        self.lose_on_renew = False

    def claim_job(self, worker_id, lease_seconds=120):
        if self.job["status"] != "queued":
            return None
        self.claim_count += 1
        self.job.update(status="running", token=f"lease-{self.claim_count}")
        return deepcopy(self.job)

    def get_run(self, run_id):
        return deepcopy(self.run)

    def _fence(self, token):
        if token != self.job["token"] or self.job["status"] != "running":
            raise ProviderError("lease_lost", "Fixture lease is no longer current")

    def update_run(self, run_id, fields, lease=None):
        self._fence((lease or {}).get("token"))
        if self.run["status"] in TERMINAL_RUNS:
            raise ProviderError("run_terminal", "Terminal fixture run cannot be changed")
        self.run.update(deepcopy(fields))
        self.publications.append(deepcopy(fields))

    def finish_job(self, job_id, token, status, result=None, error=None):
        self._fence(token)
        if self.fail_first_ack:
            self.fail_first_ack = False
            raise ProviderError("storage_unavailable", "Fixture acknowledgment connection was interrupted")
        self.acknowledgments.append({"status": status, "result": deepcopy(result), "error": deepcopy(error)})
        self.job.update(status=status, token=None)

    def renew_job(self, job_id, token, lease_seconds=120):
        self.renewals += 1
        if self.lose_on_renew:
            self.job["token"] = "replacement-worker-lease"
            return False
        return token == self.job["token"] and self.job["status"] == "running"

    def reclaim_after_crash(self):
        # Simulate the durable store making an expired running job claimable.
        assert self.job["status"] == "running"
        self.job.update(status="queued", token=None)


class CountingFixtureHub:
    def __init__(self):
        self.inference_count = 0

    async def checked_fixture_answer(self):
        self.inference_count += 1
        return "New checked fixture answer"


def test_crash_after_terminal_publication_recovers_without_repeat_inference(monkeypatch):
    class PublishingEngine:
        def __init__(self, store, hub, config):
            self.store, self.hub = store, hub

        async def run(self, job):
            answer = await self.hub.checked_fixture_answer()
            self.store.update_run("run-fixture", {"status": "answered", "answer": answer}, lease=job)
            return {"run_id": "run-fixture", "status": "answered"}

    monkeypatch.setattr(worker_module, "QueryEngine", PublishingEngine)
    store = WorkerStore(fail_first_ack=True)
    hub = CountingFixtureHub()
    worker = Worker(store, hub, load_config("configs/mock.yaml"))
    first = asyncio.run(worker.run_once())
    assert first is not None
    assert first["status"] == "failed"  # Acknowledgment failed, not publication.
    assert store.run["status"] == "answered" and store.run["answer"] == "New checked fixture answer"
    assert store.job["status"] == "running" and hub.inference_count == 1
    original_publication = deepcopy(store.run)
    store.reclaim_after_crash()
    recovered = asyncio.run(worker.run_once())
    assert recovered == {"run_id": "run-fixture", "status": "answered", "recovered": True}
    assert hub.inference_count == 1 and store.run == original_publication
    assert len(store.publications) == 1 and store.job["status"] == "succeeded"


def test_unknown_job_type_fails_safely_without_inference():
    store = WorkerStore(kind="unsupported-job")
    hub = CountingFixtureHub()
    result = asyncio.run(Worker(store, hub, load_config("configs/mock.yaml")).run_once())
    assert result is not None
    assert result["status"] == "failed" and result["code"] == "invalid_job"
    assert store.job["status"] == "failed"
    assert store.acknowledgments[0]["error"]["code"] == "invalid_job"
    assert hub.inference_count == 0


def test_heartbeat_lease_loss_cancels_operation_without_stale_publication(monkeypatch):
    original_sleep = asyncio.sleep

    async def immediate_heartbeat_sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr(worker_module.asyncio, "sleep", immediate_heartbeat_sleep)
    store = WorkerStore(kind="ingest")
    store.lose_on_renew = True
    hub = CountingFixtureHub()
    cancelled = []

    async def blocked_ingestion(job, store_arg, hub_arg, config_arg):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
        raise AssertionError("Cancelled ingestion must not continue")

    monkeypatch.setattr(worker_module, "ingest_job", blocked_ingestion)

    async def execute():
        worker = Worker(store, hub, load_config("configs/mock.yaml"))
        return await asyncio.wait_for(worker.run_once(), timeout=2)

    result = asyncio.run(execute())
    assert result is not None
    assert result["status"] == "cancelled" and result["code"] == "lease_or_shutdown"
    assert store.renewals == 1 and cancelled == [True]
    assert store.acknowledgments == [] and store.publications == []
    assert store.job["token"] == "replacement-worker-lease"


@pytest.mark.parametrize("terminal", ["verification_unavailable", "timed_out", "failed"])
def test_query_technical_terminal_maps_to_failed_job(monkeypatch, terminal):
    class TechnicalEngine:
        def __init__(self, store, hub, config):
            self.store = store

        async def run(self, job):
            self.store.update_run("run-fixture", {"status": terminal, "answer": None}, lease=job)
            return {"run_id": "run-fixture", "status": terminal, "code": "fixture_failure"}

    monkeypatch.setattr(worker_module, "QueryEngine", TechnicalEngine)
    store = WorkerStore()
    result = asyncio.run(Worker(store, CountingFixtureHub(), load_config("configs/mock.yaml")).run_once())
    assert result is not None
    assert result["status"] == terminal
    assert store.run["status"] == terminal and store.run["answer"] is None
    assert store.job["status"] == "failed"


def test_cancelled_query_is_acknowledged_as_cancelled_without_inference():
    store = WorkerStore(run_status="cancelled")
    hub = CountingFixtureHub()
    result = asyncio.run(Worker(store, hub, load_config("configs/mock.yaml")).run_once())
    assert result is not None
    assert result["status"] == "cancelled" and result["recovered"] is True
    assert store.job["status"] == "cancelled" and hub.inference_count == 0


@pytest.mark.parametrize("review_status", ["needs_review", "needs_ocr"])
def test_ingestion_review_state_is_preserved_on_job(monkeypatch, review_status):
    async def review_ingestion(job, store, hub, config):
        return {"status": review_status, "version_id": "v1"}

    monkeypatch.setattr(worker_module, "ingest_job", review_ingestion)
    store = WorkerStore(kind="ingest")
    result = asyncio.run(Worker(store, CountingFixtureHub(), load_config("configs/mock.yaml")).run_once())
    assert result is not None
    assert result["status"] == review_status and store.job["status"] == review_status


def test_unexpected_exception_is_redacted_in_logs_run_and_job(monkeypatch, caplog):
    class ExplodingEngine:
        def __init__(self, store, hub, config):
            pass

        async def run(self, job):
            raise RuntimeError("postgresql://operator:never-expose-this-password@private-host/database")

    monkeypatch.setattr(worker_module, "QueryEngine", ExplodingEngine)
    store = WorkerStore()
    result = asyncio.run(Worker(store, CountingFixtureHub(), load_config("configs/mock.yaml")).run_once())
    assert result is not None
    assert result["status"] == "failed" and result["code"] == "internal_error"
    assert store.run["answer"] is None and store.job["status"] == "failed"
    assert "never-expose-this-password" not in caplog.text + json.dumps(store.run) + json.dumps(store.acknowledgments)
    assert "The worker operation could not be completed." in store.run["error"]
