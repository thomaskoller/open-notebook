from typing import Any, Dict, List, Optional

from loguru import logger

from open_notebook.celery_app import (
    APP_NAME,
    cancel_job,
    effective_status,
    get_job,
    list_jobs,
    submit_job,
)


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a raw `command` row into the API's job representation."""
    return {
        "job_id": str(row.get("id")),
        # Not row["status"]: a job whose worker died stays non-terminal in
        # the table forever, and the drawer would count it as in progress.
        "status": effective_status(row),
        "command": row.get("command"),
        "task_id": row.get("task_id"),
        "result": row.get("result"),
        "error_message": row.get("error_message"),
        "progress": row.get("progress"),
        "attempt": row.get("attempt", 0),
        "created": str(row["created"]) if row.get("created") else None,
        "updated": str(row["updated"]) if row.get("updated") else None,
    }


class CommandService:
    """Service layer for background job operations.

    Thin wrapper over open_notebook.celery_app - jobs execute on Celery, but
    their product-facing state lives in the SurrealDB `command` table (ADR-009).
    """

    @staticmethod
    async def submit_command_job(
        module_name: str,  # app name, e.g. "open_notebook"
        command_name: str,
        command_args: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit a job for background processing. Returns the command record id."""
        return await submit_job(command_name, command_args, app=module_name or APP_NAME)

    @staticmethod
    async def get_command_status(job_id: str) -> Dict[str, Any]:
        """Get the status of a job."""
        row = await get_job(job_id)
        if not row:
            return {
                "job_id": job_id,
                "status": "unknown",
                "command": None,
                "task_id": None,
                "result": None,
                "error_message": None,
                "progress": None,
                "attempt": 0,
                "created": None,
                "updated": None,
            }
        return _serialize(row)

    @staticmethod
    async def list_command_jobs(
        module_filter: Optional[str] = None,
        command_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List jobs, newest first."""
        rows = await list_jobs(
            active_only=active_only,
            command=command_filter,
            status=status_filter,
            limit=limit,
        )
        return [_serialize(row) for row in rows]

    @staticmethod
    async def cancel_command_job(job_id: str) -> bool:
        """Cancel a queued job.

        Returns False when the job cannot be cancelled - already finished, or
        already running (the threads worker pool has no process to signal, so
        a started task runs to completion; see ADR-009).
        """
        cancelled = await cancel_job(job_id)
        if cancelled:
            logger.info(f"Cancelled job {job_id}")
        else:
            logger.info(f"Job {job_id} could not be cancelled (running or finished)")
        return cancelled
