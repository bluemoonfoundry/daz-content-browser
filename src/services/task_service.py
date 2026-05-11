from datetime import UTC, datetime, timedelta
from threading import Lock


class UpdateTaskService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._current_task: dict = {
            "running": False,
            "progress": "",
            "stage": "",
            "error": None,
            "last_run": None,
        }
        self._tasks: dict[str, dict] = {}

    def get_current(self) -> dict:
        with self._lock:
            return dict(self._current_task)

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._current_task.get("running"))

    def prune_old(self, max_age: timedelta = timedelta(hours=1)) -> None:
        cutoff = datetime.now(UTC) - max_age
        done_statuses = {0, -1}
        with self._lock:
            stale = [
                tid for tid, t in self._tasks.items()
                if t.get("task_status") in done_statuses
                and datetime.fromisoformat(t["created_at"]) < cutoff
            ]
            for tid in stale:
                del self._tasks[tid]

    def create_task(self, task_id: str) -> dict:
        task_entry = {
            "task_id": task_id,
            "task_status": 1,
            "stage": "start",
            "progress": "Task has been queued.",
            "created_at": datetime.now(UTC).isoformat(),
        }
        with self._lock:
            self._tasks[task_id] = task_entry
            self._current_task.update(
                {
                    "running": True,
                    "progress": "Starting…",
                    "stage": "start",
                    "error": None,
                    "last_run": datetime.now(UTC).isoformat(),
                }
            )
        return task_entry

    def finish_from_task_entry(self, task_entry: dict) -> None:
        with self._lock:
            self._current_task.update(
                {
                    "running": False,
                    "progress": task_entry.get("progress", ""),
                    "stage": task_entry.get("stage", ""),
                    "error": task_entry.get("progress", "") if task_entry.get("task_status") == -1 else None,
                }
            )


update_task_service = UpdateTaskService()
