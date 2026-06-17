import argparse
import logging
from managers.postgres_db_manager import main as run_daz_load

logger = logging.getLogger(__name__)

# task_status values
TASK_QUEUED   = 1
TASK_RUNNING  = 2
TASK_FINISHED = 0
TASK_ERROR    = -1


def run_update_flow(task_status: dict, force: bool = False):
    """Runs the full update flow for DAZ product data."""
    args = argparse.Namespace(force=force, all=False, phase='all', limit=None)

    def on_progress(stage: str, current: int, total: int, detail: str = ""):
        pct = int(current / total * 100) if total else 0
        label = {"etl": "Indexing", "embed": "Embedding"}.get(stage, stage.capitalize())
        task_status["stage"] = stage
        task_status["progress"] = f"{label}: {current}/{total} ({pct}%)" + (f" — {detail}" if detail else "")

    def cancel_check() -> bool:
        return bool(task_status.get("cancel_requested"))

    try:
        task_status.update({
            "task_status": TASK_RUNNING,
            "stage": "load",
            "progress": "Starting data upload...",
        })

        run_daz_load(args, on_progress=on_progress, cancel_check=cancel_check)

        if cancel_check():
            task_status.update({
                "task_status": TASK_FINISHED,
                "stage": "cancelled",
                "progress": "Indexing cancelled.",
            })
        else:
            task_status.update({
                "task_status": TASK_FINISHED,
                "stage": "finished",
                "progress": "Update process finished successfully.",
            })
    except Exception as e:
        logger.exception("Background update task failed.")
        task_status.update({
            "task_status": TASK_ERROR,
            "stage": "error",
            "progress": f"An error occurred: {e}",
        })
