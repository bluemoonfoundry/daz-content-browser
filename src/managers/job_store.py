"""SQLite-backed index job queue with a single-writer lease.

Every index run — started from the API, `vab load`, or `vab index repair` — is a row
here, so job state survives API restarts and is visible to every process. At most one
job may be queued or running at a time (a partial unique index enforces this across
processes). The process running a job holds a lease it renews by heartbeat; a job whose
lease lapses (worker killed, machine slept, terminal closed) is recovered by whichever
process notices first: re-queued while it has attempts left, otherwise failed.
"""

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

ACTIVE_STATUSES = (QUEUED, RUNNING)
DONE_STATUSES = (SUCCEEDED, FAILED, CANCELLED)

# None: from the configuration (VAB_JOB_LEASE_SECONDS / VAB_JOB_MAX_ATTEMPTS).
DEFAULT_LEASE_SECONDS: Optional[float] = None
DEFAULT_MAX_ATTEMPTS: Optional[int] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_jobs (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    params           TEXT NOT NULL DEFAULT '{}',
    source           TEXT NOT NULL DEFAULT 'api',
    status           TEXT NOT NULL,
    stage            TEXT NOT NULL DEFAULT '',
    progress         TEXT NOT NULL DEFAULT '',
    error            TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL,
    worker_id        TEXT,
    lease_expires_at REAL,
    heartbeat_at     REAL,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
-- One active job across every process: inserting or re-queuing a second one fails.
CREATE UNIQUE INDEX IF NOT EXISTS index_jobs_one_active
    ON index_jobs ((1)) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS index_jobs_created ON index_jobs (created_at);
"""


class JobConflict(Exception):
    """Another job is already queued or running."""

    def __init__(self, active: Optional[dict]):
        self.active = active
        super().__init__(f"An index job is already {active['status'] if active else 'active'}.")


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def public_view(job: Optional[dict]) -> Optional[dict]:
    """A job row shaped for API/CLI output: ISO timestamps, no lease internals."""
    if job is None:
        return None
    out = {k: v for k, v in job.items() if k != "lease_expires_at"}
    for key in ("created_at", "started_at", "finished_at", "heartbeat_at"):
        out[key] = _iso(job.get(key))
    out["cancel_requested"] = bool(job.get("cancel_requested"))
    return out


class JobStore:
    """Persisted job table. Safe to share between processes; each call opens its own connection."""

    def __init__(self, db_path: str, lease_seconds: Optional[float] = DEFAULT_LEASE_SECONDS,
                 max_attempts: Optional[int] = DEFAULT_MAX_ATTEMPTS):
        from app_config import get_config
        config = get_config()
        self.db_path = db_path
        self.lease_seconds = config.job_lease_seconds if lease_seconds is None else lease_seconds
        self.max_attempts = config.job_max_attempts if max_attempts is None else max_attempts
        self._schema_ready = False

    # ── Connection ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        # Autocommit mode; writes that must be atomic use explicit BEGIN IMMEDIATE.
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        if not self._schema_ready:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            self._schema_ready = True
        return conn

    @contextmanager
    def _conn(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _row(self, conn, job_id: str) -> Optional[dict]:
        row = conn.execute("SELECT * FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode(row)

    @staticmethod
    def _decode(row) -> Optional[dict]:
        if row is None:
            return None
        job = dict(row)
        job["params"] = json.loads(job["params"] or "{}")
        return job

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[dict]:
        with self._conn() as conn:
            return self._row(conn, job_id)

    def active(self) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM index_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()
            return self._decode(row)

    def latest(self) -> Optional[dict]:
        """The active job if there is one, else the most recently created."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM index_jobs ORDER BY status IN ('queued', 'running') DESC, created_at DESC LIMIT 1"
            ).fetchone()
            return self._decode(row)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM index_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._decode(r) for r in rows]

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT cancel_requested FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
            return bool(row and row[0])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def enqueue(self, kind: str, params: Optional[dict] = None, source: str = "api") -> dict:
        """Queues a job. Raises JobConflict if one is already queued or running."""
        self.recover_stale()
        job_id = str(uuid.uuid4())
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO index_jobs (id, kind, params, source, status, progress, max_attempts, created_at) "
                    "VALUES (?, ?, ?, ?, 'queued', 'Queued.', ?, ?)",
                    (job_id, kind, json.dumps(params or {}), source, self.max_attempts, time.time()),
                )
            except sqlite3.IntegrityError:
                raise JobConflict(self.active()) from None
            return self._row(conn, job_id)

    def claim(self, worker_id: str, job_id: Optional[str] = None) -> Optional[dict]:
        """Atomically moves the queued job (or the given one) to running under worker_id.

        Returns the claimed job, or None if there was nothing to claim — including when
        another worker won the race.
        """
        now = time.time()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if job_id is None:
                    row = conn.execute("SELECT id FROM index_jobs WHERE status = 'queued'").fetchone()
                    job_id = row[0] if row else None
                claimed = job_id is not None and conn.execute(
                    "UPDATE index_jobs SET status = 'running', worker_id = ?, attempt = attempt + 1, "
                    "lease_expires_at = ?, heartbeat_at = ?, started_at = COALESCE(started_at, ?), "
                    "stage = 'start', progress = 'Starting…' "
                    "WHERE id = ? AND status = 'queued' AND cancel_requested = 0",
                    (worker_id, now + self.lease_seconds, now, now, job_id),
                ).rowcount == 1
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return self._row(conn, job_id) if claimed else None

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Renews the lease. False means this worker no longer owns the job and must stop."""
        now = time.time()
        with self._conn() as conn:
            return conn.execute(
                "UPDATE index_jobs SET lease_expires_at = ?, heartbeat_at = ? "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (now + self.lease_seconds, now, job_id, worker_id),
            ).rowcount == 1

    def update_progress(self, job_id: str, worker_id: str, stage: str, progress: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE index_jobs SET stage = ?, progress = ? "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (stage, progress, job_id, worker_id),
            )

    def finish(self, job_id: str, worker_id: str, status: str, progress: str = "",
               error: Optional[str] = None) -> bool:
        """Records the outcome. A no-op (returns False) if the lease was lost meanwhile."""
        assert status in DONE_STATUSES
        with self._conn() as conn:
            return conn.execute(
                "UPDATE index_jobs SET status = ?, stage = ?, progress = ?, error = ?, finished_at = ?, "
                "lease_expires_at = NULL WHERE id = ? AND worker_id = ? AND status = 'running'",
                (status, status, progress, error, time.time(), job_id, worker_id),
            ).rowcount == 1

    def request_cancel(self, job_id: Optional[str] = None) -> Optional[dict]:
        """Cancels the active job (or the given one). A queued job is cancelled at once;
        a running one is flagged and stops at its worker's next checkpoint.

        Returns the job after the change, or None if there was nothing active to cancel.
        """
        now = time.time()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if job_id is None:
                    row = conn.execute("SELECT id FROM index_jobs WHERE status IN ('queued', 'running')").fetchone()
                    job_id = row[0] if row else None
                if job_id is not None:
                    conn.execute(
                        "UPDATE index_jobs SET status = 'cancelled', stage = 'cancelled', cancel_requested = 1, "
                        "progress = 'Cancelled before it started.', finished_at = ? "
                        "WHERE id = ? AND status = 'queued'",
                        (now, job_id),
                    )
                    conn.execute(
                        "UPDATE index_jobs SET cancel_requested = 1, progress = 'Cancelling…' "
                        "WHERE id = ? AND status = 'running'",
                        (job_id,),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return self._row(conn, job_id) if job_id else None

    def recover_stale(self) -> list[dict]:
        """Resolves running jobs whose lease has lapsed (their worker is gone).

        Cancelled-while-running jobs become cancelled; jobs with attempts left are re-queued
        (indexing is incremental, so a retry resumes rather than restarts); the rest fail.
        Returns the recovered jobs.
        """
        now = time.time()
        recovered = []
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stale = conn.execute(
                    "SELECT * FROM index_jobs WHERE status = 'running' AND lease_expires_at < ?", (now,)
                ).fetchall()
                for row in stale:
                    note = f"Worker {row['worker_id']} stopped responding during attempt {row['attempt']}."
                    if row["cancel_requested"]:
                        status, progress, error = CANCELLED, "Cancelled (worker exited).", None
                    elif row["attempt"] < row["max_attempts"]:
                        status, progress, error = QUEUED, f"{note} Retrying…", note
                    else:
                        status, progress, error = FAILED, note, f"{note} Gave up after {row['attempt']} attempts."
                    conn.execute(
                        "UPDATE index_jobs SET status = ?, stage = ?, progress = ?, error = ?, worker_id = NULL, "
                        "lease_expires_at = NULL, finished_at = ? WHERE id = ?",
                        (status, status, progress, error, None if status == QUEUED else now, row["id"]),
                    )
                    logger.warning(f"Recovered stale job {row['id']} → {status}: {note}")
                    recovered.append(row["id"])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return [self._row(conn, jid) for jid in recovered]
