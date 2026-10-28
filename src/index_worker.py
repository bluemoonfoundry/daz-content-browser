"""Runs index jobs from the persisted job queue (see managers/job_store.py).

The API never indexes in-process: it queues a job and spawns `vab worker --drain`, a
detached process that claims the job, renews its lease while working, and records the
outcome. Restarting or killing the API leaves the worker running; killing the worker
lets its lease lapse so the job is recovered and retried. `vab load` and
`vab index repair|rebuild` run their job inline through the same queue, so the CLI and
the API can never index at the same time.
"""

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from managers.job_store import CANCELLED, FAILED, SUCCEEDED, JobStore

logger = logging.getLogger(__name__)

_POLL_SECONDS = 2.0
_CANCEL_CHECK_SECONDS = 0.5


class JobCancelled(Exception):
    """Raised by a handler that wants to stop early because cancellation was requested."""


class JobContext:
    """What a job handler sees: its job row, progress reporting, and cancellation."""

    def __init__(self, store: JobStore, job: dict, worker_id: str,
                 extra_progress: Optional[Callable] = None):
        self.store = store
        self.job = job
        self.worker_id = worker_id
        self.lease_lost = threading.Event()
        self._extra_progress = extra_progress
        self._cancel_checked_at = 0.0
        self._cancelled = False

    @property
    def params(self) -> dict:
        return self.job["params"]

    def report(self, stage: str, progress: str) -> None:
        self.store.update_progress(self.job["id"], self.worker_id, stage, progress)

    def on_progress(self, stage: str, current: int, total: int, detail: str = "") -> None:
        """Progress callback in the ETL pipeline's (stage, current, total, detail) shape."""
        if self._extra_progress:
            self._extra_progress(stage, current, total, detail)
        pct = int(current / total * 100) if total else 0
        label = {"etl": "Indexing", "embed": "Embedding"}.get(stage, stage.capitalize())
        self.report(stage, f"{label}: {current}/{total} ({pct}%)" + (f" — {detail}" if detail else ""))

    def cancelled(self) -> bool:
        """True once cancellation is requested or this worker has lost the lease. Throttled."""
        if self._cancelled or self.lease_lost.is_set():
            return True
        now = time.monotonic()
        if now - self._cancel_checked_at >= _CANCEL_CHECK_SECONDS:
            self._cancel_checked_at = now
            self._cancelled = self.store.is_cancel_requested(self.job["id"])
        return self._cancelled


# ── Handlers ──────────────────────────────────────────────────────────────────
# Imported lazily so the worker (and the selftest) don't load ChromaDB, the embedding
# model, or PostgreSQL until a job actually needs them.

def run_index_job(ctx: JobContext) -> str:
    """ETL + embed. params: force, all, phase, limit."""
    from managers.postgres_db_manager import main as run_daz_load

    p = ctx.params
    # A retry after a crash resumes incrementally: re-running --force would wipe the
    # partial progress the failed attempt already made.
    resume = ctx.job["attempt"] > 1
    args = argparse.Namespace(
        force=bool(p.get("force")) and not resume,
        all=bool(p.get("all")),
        phase=p.get("phase", "all"),
        limit=p.get("limit"),
    )
    if resume and p.get("force"):
        ctx.report("start", "Resuming the interrupted rebuild incrementally…")
    run_daz_load(args, on_progress=ctx.on_progress, cancel_check=ctx.cancelled)
    return "Update process finished successfully."


def run_repair_job(ctx: JobContext) -> str:
    """Resets (or quarantines) the product vector store, then re-embeds from SQLite."""
    from managers.managers import chroma_db_manager
    from managers.postgres_db_manager import main as run_daz_load

    ctx.report("repair", "Resetting the vector store…")
    action = chroma_db_manager.repair()
    logger.info(action)
    if ctx.cancelled():
        raise JobCancelled()
    args = argparse.Namespace(force=False, all=False, phase="embed", limit=None)
    run_daz_load(args, on_progress=ctx.on_progress, cancel_check=ctx.cancelled)
    return f"Repair finished. {action}"


def run_selftest_job(ctx: JobContext) -> str:
    """Exercises the worker without touching any data store. params: steps, delay, fail."""
    steps = int(ctx.params.get("steps", 3))
    for i in range(steps):
        if ctx.cancelled():
            raise JobCancelled()
        ctx.on_progress("selftest", i, steps)
        time.sleep(float(ctx.params.get("delay", 0.2)))
    if ctx.params.get("fail"):
        raise RuntimeError("selftest asked to fail")
    ctx.on_progress("selftest", steps, steps)
    return "Selftest finished."


HANDLERS: dict[str, Callable[[JobContext], str]] = {
    "index": run_index_job,
    "repair": run_repair_job,
    "selftest": run_selftest_job,
}


# ── Running jobs ──────────────────────────────────────────────────────────────

def new_worker_id() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _heartbeat_loop(ctx: JobContext, stop: threading.Event) -> None:
    interval = max(ctx.store.lease_seconds / 4, 0.1)
    while not stop.wait(interval):
        try:
            if not ctx.store.heartbeat(ctx.job["id"], ctx.worker_id):
                logger.error(f"Lost the lease on job {ctx.job['id']}; stopping.")
                ctx.lease_lost.set()
                return
        except Exception:
            # A transiently locked DB shouldn't kill the job; the lease has slack for this.
            logger.exception("Heartbeat failed; will retry.")


def run_job(store: JobStore, job: dict, worker_id: str,
            extra_progress: Optional[Callable] = None) -> str:
    """Runs a claimed job to completion and records its outcome. Returns the final status."""
    ctx = JobContext(store, job, worker_id, extra_progress)
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, args=(ctx, stop), daemon=True)
    beat.start()
    status, progress, error = SUCCEEDED, "", None
    try:
        handler = HANDLERS.get(job["kind"])
        if handler is None:
            raise ValueError(f"Unknown job kind {job['kind']!r}.")
        progress = handler(ctx)
        if ctx.cancelled():
            status, progress = CANCELLED, "Indexing cancelled."
    except JobCancelled:
        status, progress = CANCELLED, "Indexing cancelled."
    except KeyboardInterrupt:
        # Ctrl+C on an inline CLI run is a cancellation, not a crash to be retried later.
        stop.set()
        beat.join()
        store.finish(job["id"], worker_id, CANCELLED, "Interrupted.")
        raise
    except Exception as e:
        logger.exception(f"Job {job['id']} ({job['kind']}) failed.")
        status, progress, error = FAILED, f"An error occurred: {e}", f"{type(e).__name__}: {e}"
    finally:
        stop.set()
        beat.join()
    if ctx.lease_lost.is_set():
        logger.warning(f"Job {job['id']} outcome not recorded: another process recovered it.")
    else:
        store.finish(job["id"], worker_id, status, progress, error)
    logger.info(f"Job {job['id']} ({job['kind']}, attempt {job['attempt']}) → {status}")
    return status


def run_inline(store: JobStore, kind: str, params: Optional[dict] = None, source: str = "cli",
               extra_progress: Optional[Callable] = None) -> Optional[dict]:
    """Queues a job and runs it in this process. Raises JobConflict if one is already active.

    Returns the finished job, or None if a background worker claimed it first (it then
    runs there; follow it with `vab jobs show`).
    """
    job = store.enqueue(kind, params, source=source)
    worker_id = new_worker_id()
    claimed = store.claim(worker_id, job["id"])
    if claimed is None:
        return None
    run_job(store, claimed, worker_id, extra_progress)
    return store.get(job["id"])


def drain(store: JobStore, worker_id: Optional[str] = None, forever: bool = False) -> int:
    """Claims and runs queued jobs until none are left (or forever). Returns jobs run."""
    worker_id = worker_id or new_worker_id()
    ran = 0
    while True:
        store.recover_stale()
        job = store.claim(worker_id)
        if job is not None:
            run_job(store, job, worker_id)
            ran += 1
            continue
        if not forever:
            return ran
        time.sleep(_POLL_SECONDS)


def worker_command(store: JobStore, forever: bool = False) -> int:
    worker_id = new_worker_id()
    logger.info(f"Index worker {worker_id} started (jobs: {store.db_path!r}).")
    ran = drain(store, worker_id, forever=forever)
    logger.info(f"Index worker {worker_id} exiting after {ran} job(s).")
    return ran


def worker_log_path(store: JobStore) -> Path:
    return Path(store.db_path).with_suffix(".worker.log")


def spawn_worker(store: JobStore) -> subprocess.Popen:
    """Starts a detached `vab worker --drain` that outlives the API process."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "worker", "--drain"]
    else:
        cmd = [sys.executable, str(Path(__file__).parent / "main.py"), "worker", "--drain"]
    env = dict(os.environ, JOBS_DB_PATH=store.db_path, PYTHONIOENCODING="utf-8")
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    log = open(worker_log_path(store), "ab")
    try:
        proc = subprocess.Popen(
            cmd, cwd=os.getcwd(), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            close_fds=True, **kwargs,
        )
    finally:
        log.close()  # the child holds its own handle
    logger.info(f"Spawned index worker pid={proc.pid}; log: {worker_log_path(store)}")
    return proc
