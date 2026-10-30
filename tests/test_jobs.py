"""Recoverable indexing: the persisted job store, the index worker (in-process and as a
real subprocess), a degraded vector store, and the API's job endpoints."""
import os
import time

import pytest
from fastapi.testclient import TestClient

import index_worker
from api_harness import ServerHarness, demo_app
from index_worker import JobContext, run_job, spawn_worker
from managers.chroma_db_manager import ChromaDbManager, VectorStoreUnavailable
from managers.job_store import CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED, JobConflict, JobStore

AUTH = {"Authorization": "Bearer test-token"}
BASE = "http://127.0.0.1"


@pytest.fixture
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"), lease_seconds=30, max_attempts=2)


def _expire_lease(store, job_id):
    with store._conn() as conn:
        conn.execute("UPDATE index_jobs SET lease_expires_at = 0 WHERE id = ?", (job_id,))


def _wait_for(predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("timed out")


# ── Job store ─────────────────────────────────────────────────────────────────

def test_only_one_active_job_across_store_instances(store):
    job = store.enqueue("selftest")
    other_process = JobStore(store.db_path)
    with pytest.raises(JobConflict) as exc:
        other_process.enqueue("index")
    assert exc.value.active["id"] == job["id"]

    store.claim("w1")
    with pytest.raises(JobConflict):
        other_process.enqueue("index")


def test_job_state_survives_restart(store):
    job = store.enqueue("index", {"force": True})
    store.claim("w1")
    store.update_progress(job["id"], "w1", "embed", "Embedding: 5/10 (50%)")

    reopened = JobStore(store.db_path)
    seen = reopened.get(job["id"])
    assert seen["status"] == RUNNING
    assert seen["params"] == {"force": True}
    assert seen["progress"] == "Embedding: 5/10 (50%)"
    assert reopened.latest()["id"] == job["id"]


def test_claim_is_won_by_exactly_one_worker(store):
    store.enqueue("selftest")
    first = store.claim("w1")
    assert first["status"] == RUNNING and first["worker_id"] == "w1" and first["attempt"] == 1
    assert store.claim("w2") is None


def test_only_the_lease_holder_can_heartbeat_or_finish(store):
    job = store.enqueue("selftest")
    store.claim("w1")
    assert store.heartbeat(job["id"], "w1")
    assert not store.heartbeat(job["id"], "intruder")
    assert not store.finish(job["id"], "intruder", SUCCEEDED)
    assert store.finish(job["id"], "w1", SUCCEEDED, "done")
    assert store.get(job["id"])["status"] == SUCCEEDED
    assert store.active() is None


def test_cancel_queued_job_is_immediate(store):
    job = store.enqueue("selftest")
    assert store.request_cancel()["status"] == CANCELLED
    assert store.claim("w1") is None
    store.enqueue("selftest")  # no longer blocks new work
    assert store.request_cancel(job["id"])["status"] == CANCELLED


def test_cancel_running_job_sets_flag(store):
    job = store.enqueue("selftest")
    store.claim("w1")
    after = store.request_cancel()
    assert after["status"] == RUNNING and after["cancel_requested"]
    assert store.is_cancel_requested(job["id"])
    assert store.request_cancel() is not None  # still active until the worker stops


def test_stale_job_is_retried_then_failed(store):
    job = store.enqueue("index")
    store.claim("w1")
    _expire_lease(store, job["id"])

    [recovered] = store.recover_stale()
    assert recovered["status"] == QUEUED
    assert "stopped responding" in recovered["error"]

    retry = store.claim("w2")
    assert retry["attempt"] == 2
    _expire_lease(store, job["id"])
    [recovered] = store.recover_stale()
    assert recovered["status"] == FAILED
    assert "Gave up after 2 attempts" in recovered["error"]


def test_stale_job_with_cancel_requested_is_cancelled(store):
    job = store.enqueue("index")
    store.claim("w1")
    store.request_cancel()
    _expire_lease(store, job["id"])
    [recovered] = store.recover_stale()
    assert recovered["status"] == CANCELLED


def test_enqueue_recovers_a_dead_job_first(store):
    job = store.enqueue("index")
    store.claim("w1")
    store.request_cancel()
    _expire_lease(store, job["id"])
    assert store.enqueue("index")["status"] == QUEUED


# ── Worker, in-process ────────────────────────────────────────────────────────

def _claimed(store, kind, params=None):
    job = store.enqueue(kind, params)
    return store.claim("w1", job["id"])


def test_run_job_success_records_progress(store):
    job = _claimed(store, "selftest", {"steps": 2, "delay": 0})
    assert run_job(store, job, "w1") == SUCCEEDED
    done = store.get(job["id"])
    assert done["progress"] == "Selftest finished."
    assert done["finished_at"] is not None


def test_run_job_failure_is_observable(store):
    job = _claimed(store, "selftest", {"steps": 1, "delay": 0, "fail": True})
    assert run_job(store, job, "w1") == FAILED
    done = store.get(job["id"])
    assert "selftest asked to fail" in done["error"]
    assert done["progress"].startswith("An error occurred")


def test_run_job_unknown_kind_fails(store):
    job = _claimed(store, "bogus")
    assert run_job(store, job, "w1") == FAILED


def test_run_job_honours_cancellation(store, monkeypatch):
    monkeypatch.setattr(index_worker, "_CANCEL_CHECK_SECONDS", 0)
    job = _claimed(store, "selftest", {"steps": 50, "delay": 0.01})
    store.request_cancel(job["id"])
    assert run_job(store, job, "w1") == CANCELLED
    assert store.get(job["id"])["status"] == CANCELLED


def test_lost_lease_stops_job_without_recording(store):
    job = _claimed(store, "selftest")
    ctx = JobContext(store, job, "w1")
    _expire_lease(store, job["id"])
    store.recover_stale()  # another process takes the job back
    assert not store.heartbeat(job["id"], "w1")
    ctx.lease_lost.set()
    assert ctx.cancelled()


@pytest.fixture
def recorded_handlers(monkeypatch):
    calls = []

    def handler(ctx):
        calls.append((ctx.job["kind"], ctx.job["attempt"], dict(ctx.params)))
        return "ok"

    for kind in ("index", "rebuild", "repair"):
        monkeypatch.setitem(index_worker.HANDLERS, kind, handler)
    return calls


def test_rebuild_retry_keeps_its_params(store, recorded_handlers):
    # A retry after a crash re-runs the same rebuild: the building generation (and its
    # refetch_started_at marker) makes it resume instead of starting over.
    job = store.enqueue("rebuild", {"refetch": True})
    store.claim("w1")
    _expire_lease(store, job["id"])
    store.recover_stale()
    retry = store.claim("w2")
    assert run_job(store, retry, "w2") == SUCCEEDED
    assert recorded_handlers == [("rebuild", 2, {"refetch": True})]


def test_keyboard_interrupt_records_resume_hint(store, monkeypatch):
    def interrupted(ctx):
        raise KeyboardInterrupt

    monkeypatch.setitem(index_worker.HANDLERS, "rebuild", interrupted)
    job = _claimed(store, "rebuild", {"refetch": False})
    with pytest.raises(KeyboardInterrupt):
        run_job(store, job, "w1")
    done = store.get(job["id"])
    assert done["status"] == CANCELLED
    assert done["progress"] == "Interrupted — run the same command again to resume."


# ── Worker, as a real subprocess ──────────────────────────────────────────────

@pytest.fixture
def subprocess_store(tmp_path, monkeypatch):
    # Short lease so a killed worker is noticed quickly; inherited by spawned workers.
    monkeypatch.setenv("VAB_JOB_LEASE_SECONDS", "1")
    return JobStore(str(tmp_path / "jobs.db"), lease_seconds=1, max_attempts=3)


def test_worker_subprocess_smoke(subprocess_store):
    job = subprocess_store.enqueue("selftest", {"steps": 3, "delay": 0.05})
    proc = spawn_worker(subprocess_store)
    assert proc.wait(timeout=120) == 0
    done = subprocess_store.get(job["id"])
    assert done["status"] == SUCCEEDED, index_worker.worker_log_path(subprocess_store).read_text()
    # Ran in another process (not proc.pid exactly: Windows venv python.exe is a launcher)
    assert not done["worker_id"].startswith(f"{os.getpid()}-")


def test_worker_subprocess_cancel(subprocess_store):
    job = subprocess_store.enqueue("selftest", {"steps": 600, "delay": 0.1})
    proc = spawn_worker(subprocess_store)
    _wait_for(lambda: subprocess_store.get(job["id"])["status"] == RUNNING)
    subprocess_store.request_cancel()
    assert proc.wait(timeout=60) == 0
    assert subprocess_store.get(job["id"])["status"] == CANCELLED


def test_killed_worker_job_is_recovered_and_finished(subprocess_store):
    job = subprocess_store.enqueue("selftest", {"steps": 600, "delay": 0.1})
    proc = spawn_worker(subprocess_store)
    _wait_for(lambda: subprocess_store.get(job["id"])["status"] == RUNNING)
    proc.kill()
    proc.wait(timeout=30)

    _wait_for(lambda: subprocess_store.recover_stale(), timeout=10)
    assert subprocess_store.get(job["id"])["status"] == QUEUED

    # Shorten the retry so the second worker finishes quickly.
    with subprocess_store._conn() as conn:
        conn.execute("UPDATE index_jobs SET params = '{\"steps\": 2, \"delay\": 0}' WHERE id = ?", (job["id"],))
    assert spawn_worker(subprocess_store).wait(timeout=120) == 0
    done = subprocess_store.get(job["id"])
    assert done["status"] == SUCCEEDED
    assert done["attempt"] == 2


# ── Vector store degradation ──────────────────────────────────────────────────

@pytest.fixture
def broken_chroma(tmp_path):
    # A file where the store directory should be: chromadb cannot open it.
    path = tmp_path / "chroma_db"
    path.write_text("not a chroma store", encoding="utf-8")
    return ChromaDbManager(str(path), "daz_products2")


def test_unopenable_store_does_not_raise_until_used(broken_chroma):
    health = broken_chroma.health()
    assert health["ok"] is False
    assert "vab index repair" in health["repair"]
    with pytest.raises(VectorStoreUnavailable):
        broken_chroma.collection


def test_store_recovers_in_process_once_fixed(broken_chroma):
    # e.g. the worker process repaired it while the API kept running
    assert broken_chroma.health()["ok"] is False
    os.remove(broken_chroma.chroma_db_path)
    assert broken_chroma.health()["ok"] is True


def test_healthy_store_reports_count(tmp_path):
    health = ChromaDbManager(str(tmp_path / "chroma_db"), "daz_products2").health()
    assert health == {"ok": True, "path": str(tmp_path / "chroma_db"), "collection": "daz_products2", "count": 0}


def test_repair_quarantines_an_unopenable_store(broken_chroma, tmp_path):
    action = broken_chroma.repair()
    assert "moved it to" in action
    assert broken_chroma.health()["ok"] is True
    assert any(p.name.startswith("chroma_db.corrupt-") for p in tmp_path.iterdir())


# ── API, production mode ──────────────────────────────────────────────────────

@pytest.fixture
def prod(monkeypatch, tmp_path, broken_chroma, fake_embedder):
    from catalog.active_index import ActiveIndex
    from catalog.documents import DOC_FORMAT_VERSION
    from catalog.generations import GenerationStore
    from catalog.schema import LATEST_VERSION
    from catalog.store import CatalogStore

    # An active generation whose collection lives in the unopenable store.
    monkeypatch.setenv("EMBEDDING_MODEL_OVERRIDE", "fake/model-a")
    db = str(tmp_path / "catalog.db")
    catalog = CatalogStore(db)
    gens = GenerationStore(db)
    gens.create_active_adopted("daz_products2", "fake/model-a", 8, DOC_FORMAT_VERSION, LATEST_VERSION)

    spawned = []
    # No DAZ CMS: never reach a real one that .env may configure.
    server = ServerHarness(catalog, gens, ActiveIndex(gens, broken_chroma.chroma_db_path),
                           JobStore(str(tmp_path / "api_jobs.db")))
    server.spawn_worker = lambda store: spawned.append(store)
    server.spawned = spawned
    return server


def test_api_starts_and_reports_degraded_with_broken_store(prod):
    with TestClient(prod.app, base_url=BASE, headers=AUTH) as client:  # runs the lifespan
        status = client.get("/api/v1/status").json()
        assert status["status"] == "degraded"
        assert status["chromadb_connected"] is False
        assert "vab index repair" in status["repair"]

        health = client.get("/api/v1/health").json()
        assert health["status"] == "degraded"
        assert health["components"]["vector_store"]["ok"] is False

        r = client.post("/api/v1/search", json={"query": "dress"})
        assert r.status_code == 503
        assert "vab index repair" in r.json()["repair"]

        assert client.get("/api/v1/products").status_code == 200  # browse still works


def test_api_update_lifecycle(prod):
    client = TestClient(prod.app, base_url=BASE, headers=AUTH)
    assert client.get("/api/v1/update/status").json()["running"] is False

    r = client.post("/api/v1/update", json={"mode": "repair"})
    assert r.status_code == 202
    task_id = r.json()["task_id"]
    assert len(prod.spawned) == 1
    assert prod.job_store.get(task_id)["kind"] == "repair"

    again = client.post("/api/v1/update", json={"force": True}).json()
    assert again["message"] == "Update already running."
    assert again["task_id"] == task_id

    status = client.get("/api/v1/update/status").json()
    assert status["running"] is True and status["task_status"] == 1 and status["task_id"] == task_id
    assert client.get(f"/api/v1/update/status/{task_id}").json()["status"] == QUEUED
    assert client.get("/api/v1/jobs").json()["jobs"][0]["id"] == task_id

    assert client.delete("/api/v1/update").json()["status"] == CANCELLED
    status = client.get("/api/v1/update/status").json()
    assert status["running"] is False and status["status"] == CANCELLED


@pytest.mark.parametrize("body, kind, params", [
    ({"mode": "update"}, "index", {}),
    ({}, "index", {}),
    ({"mode": "rebuild"}, "rebuild", {"refetch": False}),
    ({"force": True}, "rebuild", {"refetch": True}),
    ({"mode": "repair"}, "repair", {}),
])
def test_api_update_modes_map_to_kinds(prod, body, kind, params):
    client = TestClient(prod.app, base_url=BASE, headers=AUTH)
    r = client.post("/api/v1/update", json=body)
    assert r.status_code == 202
    job = prod.job_store.get(r.json()["task_id"])
    assert (job["kind"], job["params"]) == (kind, params)


def test_api_update_spawn_failure_does_not_wedge_the_queue(prod, monkeypatch):
    def boom(store):
        raise OSError("no python")

    monkeypatch.setattr(prod, "spawn_worker", boom)
    client = TestClient(prod.app, base_url=BASE, headers=AUTH)
    assert client.post("/api/v1/update", json={}).status_code == 500
    latest = prod.job_store.latest()
    assert latest["status"] == FAILED and "no python" in latest["error"]
    assert client.get("/api/v1/update/status").json()["error"]

    monkeypatch.setattr(prod, "spawn_worker", lambda store: None)
    assert client.post("/api/v1/update", json={}).status_code == 202


def test_update_status_unknown_task_404(tmp_path):
    client = TestClient(demo_app(tmp_path), base_url=BASE, headers=AUTH)
    assert client.get("/api/v1/update/status/nope").status_code == 404
