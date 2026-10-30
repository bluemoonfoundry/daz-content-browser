"""Runs index jobs from the persisted job queue (see managers/job_store.py).

The API never indexes in-process: it queues a job and spawns `vab worker --drain`, a
detached process that claims the job, renews its lease while working, and records the
outcome. Restarting or killing the API leaves the worker running; killing the worker
lets its lease lapse so the job is recovered and retried (a rebuild resumes its building
generation). `vab load` and
`vab index repair|rebuild` run their job inline through the same queue, so the CLI and
the API can never index at the same time.
"""

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

_CMS_UNAVAILABLE = (
    "DAZ CMS PostgreSQL is not available — check DB_NAME, DB_USER, DB_PASS, DB_HOST and "
    "DB_PORT (or the CMS connection in Settings) and that DAZ Studio's CMS is running."
)
# Before quarantining a store that won't open, wait this long and try once more, so a
# transient failure (e.g. a file briefly locked) doesn't move a healthy store aside.
_REOPEN_PAUSE_SECONDS = 1.0


def _resume_hint(gen: dict) -> str:
    """The exact command that resumes ``gen`` (another model or refetch flag would discard it)."""
    cmd = f"vab index rebuild --model {gen['model_id']}"
    if gen.get("refetch_started_at"):
        cmd += " --refetch"
    return f"The new generation is kept; resume with `{cmd}`."
# embed_pending takes one snapshot per call; a rebuild makes up to this many passes so
# failed batches are retried within their budget (GenerationStore.pending's max_attempts).
_EMBED_PASSES = 3


def _managers():
    from managers import managers
    return managers


def _figure_names() -> list:
    from catalog.documents import load_figure_names
    return load_figure_names()


def _scraper(ctx: JobContext) -> Callable[[list], dict]:
    """The store scraper the reconciler calls per batch of SKUs."""
    from managers.postgres_db_manager import scrape_many

    concurrency = int(os.getenv("ETL_SCRAPE_CONCURRENCY", "10"))

    def progress(stage, current, total, detail=""):
        ctx.on_progress("etl", current, total, str(detail))

    def scrape(skus: list) -> dict:
        return scrape_many(skus, concurrency=concurrency, on_progress=progress)
    return scrape


def _check_migrated() -> None:
    error = _managers().catalog.migration_error
    if error:
        raise RuntimeError(error)


def _prepare() -> None:
    """Refuses to run on an unmigrated catalog; adopts a legacy index; drops retired ones."""
    from catalog.model_config import configured_model_id

    _check_migrated()
    m = _managers()
    m.builder.adopt_legacy(configured_model_id())
    m.builder.cleanup_retired()


def _reconcile(ctx: JobContext, **kwargs):
    from catalog.reconcile import reconcile_daz

    m = _managers()
    if m.daz_pg_analyzer is None:
        raise RuntimeError(_CMS_UNAVAILABLE)
    ctx.report("etl", "Reading the DAZ CMS product list…")
    cms_rows = m.daz_pg_analyzer.get_all_products()
    return reconcile_daz(m.catalog, cms_rows, _scraper(ctx), _figure_names(),
                         cancel_check=ctx.cancelled, **kwargs)


def _reconcile_summary(r) -> str:
    return (f"{r.new} new, {r.changed} changed, {r.removed} removed, {r.reappeared} reappeared, "
            f"{r.refreshed_payloads} refreshed, {r.scraped} scraped, {r.scrape_errors} scrape errors")


def _embed_summary(e: dict) -> str:
    return (f"{e['embedded']} embedded, {e['errors']} embed errors, "
            f"{e['orphans_removed']} orphans removed")


def _check_cancel(ctx: JobContext, gen: dict) -> None:
    """At a rebuild checkpoint: an explicit cancel discards the building generation.

    A lost lease means another worker recovered the job and may be resuming the same
    generation, so it is left alone (stopping is enough).
    """
    if not ctx.cancelled():
        return
    if not ctx.lease_lost.is_set():
        _managers().builder.discard(gen, "cancelled")
    raise JobCancelled()


def _embed_all(ctx: JobContext, gen: dict) -> dict:
    """Embeds ``gen`` until nothing is pending or the retry budget is spent."""
    m = _managers()
    totals = {"embedded": 0, "errors": 0, "orphans_removed": 0}
    for _ in range(_EMBED_PASSES):
        result = m.builder.embed_pending(gen, cancel_check=ctx.cancelled,
                                         on_progress=ctx.on_progress)
        totals["embedded"] += result["embedded"]
        totals["orphans_removed"] += result["orphans_removed"]
        totals["errors"] = result["errors"]  # failures still outstanding after this pass
        _check_cancel(ctx, gen)
        if not m.generations.pending(gen["id"]):
            break
    return totals


def _rebuild(ctx: JobContext, model_id: str, refetch: bool) -> str:
    """Builds (or resumes) a generation for ``model_id`` beside the active one, then promotes it.

    ``KeyboardInterrupt`` and crashes are not caught: the building generation survives
    and the next rebuild for the same model resumes it.
    """
    from catalog.builder import RebuildIncomplete

    m = _managers()
    if refetch and m.daz_pg_analyzer is None:
        raise RuntimeError(_CMS_UNAVAILABLE)  # before creating a generation it can't finish
    ctx.report("rebuild", f"Preparing a new index generation for {model_id}…")
    gen = m.builder.start_or_resume_rebuild(model_id, refetch=refetch)
    _check_cancel(ctx, gen)
    # A refetch generation always finishes its refetch, whichever command resumes it.
    refetch = refetch or bool(gen.get("refetch_started_at"))
    parts = []
    if refetch:
        # A resumed generation that was started without refetch has no marker: anything
        # fetched before it was created counts as not yet refreshed.
        marker = gen["refetch_started_at"] or gen["created_at"]
        parts.append(_reconcile_summary(_reconcile(ctx, refetch_before=marker)))
        _check_cancel(ctx, gen)
    parts.append(_embed_summary(_embed_all(ctx, gen)))
    remaining = m.generations.pending(gen["id"])
    if remaining:
        raise RuntimeError(f"Generation {gen['id']} still has {len(remaining)} pending "
                           f"asset(s) after this run. {_resume_hint(gen)}")
    try:
        m.builder.finish_rebuild(gen)
    except RebuildIncomplete as e:
        raise RuntimeError(f"{e} {_resume_hint(gen)}") from e
    return f"Generation {gen['id']} ({model_id}) promoted: " + "; ".join(parts) + "."


def run_index_job(ctx: JobContext) -> str:
    """Incremental run: reconcile the DAZ CMS, then embed into the active generation.

    params: all, phase (etl|embed|all), limit, model. ``model`` only picks the model of the
    first generation; with an active generation its own model is always used (rebuilds are
    never started automatically). Cancelling just stops: partial work stays and the
    active generation is untouched.
    """
    from catalog.model_config import configured_model_id

    _prepare()
    m = _managers()
    p = ctx.params
    phase = p.get("phase") or "all"
    limit = p.get("limit")
    parts = []
    if phase != "embed":
        parts.append(_reconcile_summary(
            _reconcile(ctx, refetch_all=bool(p.get("all")), limit=limit)))
        if ctx.cancelled():
            raise JobCancelled()
    if phase != "etl":
        active = m.generations.active()
        if active is None:
            parts.append(_rebuild(ctx, p.get("model") or configured_model_id(), refetch=False))
        else:
            if p.get("model") and p["model"] != active["model_id"]:
                logger.warning("The active generation uses %s, not %s; embedding with %s. Run "
                               "`vab index rebuild --model %s` to switch models.",
                               active["model_id"], p["model"], active["model_id"], p["model"])
            embedded = m.builder.embed_pending(active, cancel_check=ctx.cancelled,
                                               on_progress=ctx.on_progress, limit=limit)
            if ctx.cancelled():
                raise JobCancelled()
            parts.append(_embed_summary(embedded))
    return "Index updated: " + "; ".join(parts) + "."


def run_rebuild_job(ctx: JobContext) -> str:
    """New generation from SQLite (re-fetching store data first if ``refetch``), then promote.

    params: refetch, model.
    """
    from catalog.model_config import configured_model_id

    _prepare()
    p = ctx.params
    return _rebuild(ctx, p.get("model") or configured_model_id(), refetch=bool(p.get("refetch")))


def run_repair_job(ctx: JobContext) -> str:
    """Quarantines a store that won't open at all, then rebuilds from SQLite and promotes.

    Never resets or deletes a collection a generation references: the old active one keeps
    serving until the new generation replaces it, and is then cleaned up as retired.
    """
    from catalog.model_config import configured_model_id
    from managers.chroma_db_manager import quarantine_store, store_open_error

    _check_migrated()
    m = _managers()
    action = "The vector store opened; rebuilt the index beside it."
    chroma_path = m.builder.chroma_path
    if os.path.exists(chroma_path):
        error = store_open_error(chroma_path)
        if error is not None:
            logger.warning("The vector store will not open (%s); retrying once.", error)
            time.sleep(_REOPEN_PAUSE_SECONDS)
            error = store_open_error(chroma_path)
        if error is not None:
            ctx.report("repair", "The vector store will not open; quarantining it…")
            moved = quarantine_store(chroma_path, error)
            action = (f"The vector store could not be opened; moved it to {moved!r}. Other "
                      "collections in it (e.g. morphs) must be re-indexed.")
    _prepare()
    if ctx.cancelled():
        raise JobCancelled()
    active = m.generations.active()
    model_id = active["model_id"] if active else configured_model_id()
    return f"Repair finished. {action} " + _rebuild(ctx, model_id, refetch=False)


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
    "rebuild": run_rebuild_job,
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
        # Handlers never catch it, so a building generation survives and the same
        # command resumes it.
        stop.set()
        beat.join()
        store.finish(job["id"], worker_id, CANCELLED,
                     "Interrupted — run the same command again to resume.")
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
