"""Index jobs as the API sees them.

Jobs live in the persisted job store and run in a separate worker process, so they
survive API restarts and only one can run across processes (see index_worker.py).
"""
import logging
import time
from typing import Callable, Literal, Optional

from managers.job_store import FAILED, QUEUED, RUNNING, JobConflict, public_view
from services.errors import ActionFailed, Forbidden

logger = logging.getLogger(__name__)

UpdateMode = Literal["update", "rebuild", "repair"]

_RESPAWN_SECONDS = 30.0
_TASK_CODES = {QUEUED: 1, RUNNING: 2, FAILED: -1}
_KINDS = {
    "update": ("index", {}),
    "rebuild": ("rebuild", {"refetch": False}),  # new generation from SQLite
    "repair": ("repair", {}),
}


def legacy_task_view(job: dict) -> dict:
    """A job plus the fields the UI and older clients poll (running/progress/error/task_status)."""
    view = public_view(job)
    view.update({
        "task_id": job["id"],
        "task_status": _TASK_CODES.get(job["status"], 0),
        "running": job["status"] in (QUEUED, RUNNING),
        "last_run": view["created_at"],
    })
    if job["status"] != FAILED:
        view["error"] = None
    return view


class JobService:
    def __init__(self, job_store, spawn_worker: Callable, enabled: bool = True):
        """``spawn_worker(job_store)`` starts a detached worker; ``enabled`` is False in demo
        mode, where no worker is reconciled or started."""
        self.job_store = job_store
        self._spawn = spawn_worker
        self.enabled = enabled
        self._last_spawn = 0.0

    def _spawn_worker(self) -> None:
        self._last_spawn = time.monotonic()
        self._spawn(self.job_store)

    def reconcile(self) -> None:
        """Recovers jobs whose worker died and makes sure a queued job has a worker.

        Called at startup and on status polls, so a crashed worker is noticed without
        waiting for an API restart. Respawns are rate-limited: a worker that was just
        spawned may simply not have claimed its job yet.
        """
        if not self.enabled:
            return
        try:
            recovered = self.job_store.recover_stale()
            active = self.job_store.active()
            if active and active["status"] == QUEUED and (
                recovered or time.monotonic() - self._last_spawn > _RESPAWN_SECONDS
            ):
                self._spawn_worker()
        except Exception:
            logger.exception("Could not reconcile index jobs.")

    def update_status(self) -> dict:
        """The active job, or else the most recent one, in the legacy shape."""
        self.reconcile()
        latest = self.job_store.latest()
        if latest is None:
            return {"running": False, "progress": "", "stage": "", "error": None, "last_run": None}
        return legacy_task_view(latest)

    def task(self, task_id: str) -> Optional[dict]:
        job = self.job_store.get(task_id)
        return legacy_task_view(job) if job else None

    def recent(self, limit: int) -> list:
        return [public_view(j) for j in self.job_store.recent(limit)]

    def start(self, mode: Optional[UpdateMode], force: bool) -> dict:
        """Queues an index job and starts a worker for it; returns the active job instead
        while one is already queued or running."""
        if not self.enabled:
            raise Forbidden("Update functionality is disabled in demo mode.")
        if mode is None and force:
            # Legacy spelling: re-fetch every product's store data, then build a new generation.
            mode, kind, params = "rebuild", "rebuild", {"refetch": True}
        else:
            mode = mode or "update"
            kind, params = _KINDS[mode]
        try:
            job = self.job_store.enqueue(kind, params, source="api")
        except JobConflict as e:
            return {"message": "Update already running.",
                    "task_id": e.active["id"] if e.active else None, "mode": None}
        try:
            self._spawn_worker()
        except Exception as e:
            # Never leave a queued job that nothing will run: it would block every later update.
            logger.exception("Could not start the index worker.")
            self.job_store.claim("api", job["id"])
            self.job_store.finish(job["id"], "api", FAILED,
                                  f"Could not start the index worker: {e}", str(e))
            raise ActionFailed(f"Could not start the index worker: {e}")
        return {"message": "Update process started.", "task_id": job["id"], "mode": mode}

    def cancel(self) -> dict:
        """Cancels the active job: at once if still queued, else at the worker's next checkpoint."""
        job = self.job_store.request_cancel()
        if job is None:
            return {"message": "No update is running.", "task_id": None, "status": None}
        return {"message": "Cancel requested.", "task_id": job["id"], "status": job["status"]}

    def health(self, worker_log: str) -> dict:
        self.reconcile()
        latest = self.job_store.latest()
        return {"ok": not (latest and latest["status"] == FAILED),
                "latest": public_view(latest), "worker_log": worker_log}
