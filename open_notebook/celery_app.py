"""Celery application — the background job runtime.

Replaces surreal-commands (see ADR-009). The split of responsibilities:

* **Celery executes.** Redis is the broker; the worker runs the task bodies.
  Flower (``make flower``) reads the same broker for operational observability:
  queue depth, in-flight tasks, retries, worker health.
* **SurrealDB records.** The ``command`` table stays the product-facing job
  record. ``source.command`` and ``episode.command`` are typed
  ``option<record<command>>`` by migrations 8 and 7, ``GET /api/sources``
  resolves status inline via ``FETCH command``, and the frontend polls that
  state — none of which a Celery task UUID could satisfy. The signal handlers
  below mirror every Celery state transition onto that row, promptly, so the
  UI sees ``queued -> running -> progress -> completed`` as it happens.

Task bodies are ``async def`` (the codebase is async-first) while Celery tasks
are sync, so :func:`async_task` bridges the two with ``asyncio.run`` per task.
The repository layer opens a connection per call and holds no pooled state, so
a fresh loop per task costs nothing.
"""

import asyncio
import inspect
import os
import typing
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

# Must run before anything opens a socket to the DB or the broker: internal
# hosts (surrealdb, redis, localhost) must never be tunnelled through a
# configured HTTP proxy, or the connection dies with a 403 (issue #1160).
from open_notebook.utils.proxy import ensure_internal_no_proxy

ensure_internal_no_proxy()

from celery import Celery  # noqa: E402
from celery.exceptions import Ignore  # noqa: E402
from celery.signals import (  # noqa: E402
    task_failure,
    task_retry,
    task_revoked,
    task_success,
)
from loguru import logger  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from open_notebook.database.repository import (  # noqa: E402
    ensure_record_id,
    repo_create,
    repo_query,
    repo_update,
)

APP_NAME = "open_notebook"
COMMAND_TABLE = "command"

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# ---------------------------------------------------------------------------
# Job status vocabulary
#
# `queued`/`running`/`completed`/`failed` are the strings surreal-commands
# wrote and that the frontend, the source status endpoint and the podcast
# episode listing already branch on. Keep them verbatim; `retrying` and
# `cancelled` are new states Celery makes observable.
# ---------------------------------------------------------------------------
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
RETRYING = "retrying"
CANCELLED = "cancelled"

UNKNOWN = "unknown"

ACTIVE_STATUSES = (QUEUED, RUNNING, RETRYING)
TERMINAL_STATUSES = (COMPLETED, FAILED, CANCELLED)

# How long a non-terminal row may go untouched before it is reported as
# `unknown` rather than as still-in-progress.
STALE_JOB_MINUTES = int(os.environ.get("OPEN_NOTEBOOK_JOB_STALE_MINUTES", "30"))


def effective_status(row: Dict[str, Any]) -> str:
    """The row's status, reporting an abandoned job as `unknown`.

    Every terminal transition is written by a Celery signal *in the worker*, so
    a worker that dies mid-task (container restart, OOM, SIGKILL) leaves its
    row `running` forever, and a queued message lost with the broker leaves it
    `queued` forever. Nothing inside the dead process can reconcile that, so
    the reader does: a non-terminal row nobody has touched in
    STALE_JOB_MINUTES is not in progress, and counting it as such makes the
    jobs indicator claim work that will never finish.

    Read-only on purpose. The row is left alone, so a job that merely went
    quiet for a while (a long extraction between progress reports) shows up as
    active again the moment it writes anything, and no state is destroyed by a
    threshold that turns out to be too tight.
    """
    status = row.get("status") or UNKNOWN
    if status not in ACTIVE_STATUSES:
        return status

    # `updated` is refreshed by every repo_update (status transitions and
    # progress reports); started_at/created cover a row that has not been
    # written since it was claimed or created.
    last_seen = max(
        (
            value
            for value in (row.get("updated"), row.get("started_at"), row.get("created"))
            if isinstance(value, datetime)
        ),
        default=None,
    )
    if last_seen is None:
        return status
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - last_seen
    if age > timedelta(minutes=STALE_JOB_MINUTES):
        return UNKNOWN
    return status


celery = Celery(
    APP_NAME,
    broker=REDIS_URL,
    backend=REDIS_URL,
    # Importing `commands` is what registers the task bodies. Declaring it here
    # (rather than as a --include flag) means `celery -A
    # open_notebook.celery_app:celery` works for the worker, for Flower's
    # registered-task list, and for `celery inspect` without extra arguments.
    # Celery imports these lazily on finalize, so there is no circular import
    # with the command modules importing this one.
    include=["commands"],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Flower renders STARTED and the live task list from these events.
    task_track_started=True,
    worker_send_task_events=True,
    task_send_sent_event=True,
    # Ack after the task finishes so a killed worker redelivers the job
    # instead of losing it. Prefetch 1 keeps a slow task from hoarding the
    # queue on a single-worker self-hosted install.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Celery's own result rows are throwaway — the command table is the record
    # of truth, and Flower only needs them while a job is recent.
    result_expires=3600,
    broker_connection_retry_on_startup=True,
)


# ---------------------------------------------------------------------------
# Task input / output base models
#
# Drop-in replacements for surreal-commands' CommandInput / CommandOutput, so
# the command modules keep their `input_data.execution_context.command_id`
# idiom unchanged.
# ---------------------------------------------------------------------------


class ExecutionContext(BaseModel):
    command_id: Optional[str] = None


class TaskInput(BaseModel):
    execution_context: Optional[ExecutionContext] = None


class TaskOutput(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Writing job state onto the command row
# ---------------------------------------------------------------------------


async def _update_command(command_id: str, fields: Dict[str, Any]) -> None:
    await repo_update(COMMAND_TABLE, ensure_record_id(command_id), fields)


def _record_state(command_id: Optional[str], **fields: Any) -> None:
    """Best-effort sync write of a state transition (called from signals).

    Signal handlers are synchronous and the repository is async-only, so this
    spins a short-lived loop. Handlers run sequentially in the worker thread
    (before/after the task body's own ``asyncio.run``), so there is never a
    loop already running here.

    Never raises: losing a status write must not fail an otherwise good job.
    The job still completes; the UI just misses one transition.
    """
    if not command_id:
        return
    try:
        asyncio.run(_update_command(command_id, fields))
    except Exception as e:
        logger.warning(f"Failed to record job state for {command_id}: {e}")


async def report_progress(
    command_id: Optional[str],
    *,
    message: str,
    current: Optional[int] = None,
    total: Optional[int] = None,
) -> None:
    """Publish intermediate progress for a running job.

    This is what turns a multi-minute job from a spinner into something the
    jobs drawer can actually narrate. Call it at stage boundaries (and, for
    fan-out work, every N items — not every item; each call is a DB write).
    """
    if not command_id:
        return
    progress: Dict[str, Any] = {"message": message}
    if current is not None:
        progress["current"] = current
    if total is not None:
        progress["total"] = total
        if total > 0 and current is not None:
            progress["percent"] = round(100 * current / total)
    try:
        await _update_command(command_id, {"progress": progress})
    except Exception as e:
        logger.warning(f"Failed to report progress for {command_id}: {e}")


def _command_id_from(kwargs: Optional[Dict[str, Any]]) -> Optional[str]:
    return (kwargs or {}).get("command_id")


async def _claim(command_id: str) -> bool:
    """Mark the job running, unless it was cancelled. Returns False if it was.

    ``celery.control.revoke`` is a broadcast to *running* workers and is not
    remembered: cancel a queued job while the worker is down (or restart the
    worker before it drains) and the task is delivered and executed anyway.
    The command row is the record of truth, so the worker re-checks it here.

    One conditional UPDATE, so the check and the claim cannot interleave.
    """
    rows = await repo_query(
        "UPDATE $id SET status = $running, started_at = time::now() "
        "WHERE status != $cancelled RETURN AFTER",
        {
            "id": ensure_record_id(command_id),
            "running": RUNNING,
            "cancelled": CANCELLED,
        },
    )
    return bool(rows)


def _claim_job(command_id: Optional[str]) -> bool:
    """Sync wrapper for :func:`_claim`. Never blocks a job on its own failure."""
    if not command_id:
        return True
    try:
        return asyncio.run(_claim(command_id))
    except Exception as e:
        # Losing the claim write costs visibility, not the user's work - run it.
        logger.warning(f"Failed to claim job {command_id}: {e}")
        return True


@task_success.connect
def _on_success(sender=None, result=None, **_):
    request = getattr(sender, "request", None)
    _record_state(
        _command_id_from(getattr(request, "kwargs", None)),
        status=COMPLETED,
        result=result,
        error_message=None,
        completed_at=datetime.now(timezone.utc),
    )


@task_failure.connect
def _on_failure(task_id=None, exception=None, kwargs=None, **_):
    _record_state(
        _command_id_from(kwargs),
        status=FAILED,
        error_message=str(exception),
        completed_at=datetime.now(timezone.utc),
    )


@task_retry.connect
def _on_retry(sender=None, request=None, reason=None, **_):
    command_id = _command_id_from(getattr(request, "kwargs", None))
    attempt = (getattr(request, "retries", 0) or 0) + 1
    # Retries must be visible in ordinary worker logs — a silently retrying
    # provider call looks identical to a hung job otherwise.
    level = str(getattr(sender, "retry_log_level", "warning")).upper()
    logger.log(
        level,
        f"[Retry] Attempt {attempt} of {getattr(sender, 'name', '?')} failed with "
        f"{type(reason).__name__}: {reason}",
    )
    _record_state(
        command_id,
        status=RETRYING,
        attempt=attempt,
        error_message=str(reason),
    )


@task_revoked.connect
def _on_revoked(request=None, **_):
    _record_state(
        _command_id_from(getattr(request, "kwargs", None)),
        status=CANCELLED,
        completed_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# The async -> Celery bridge
# ---------------------------------------------------------------------------


def _input_model(fn: Callable) -> type:
    """The pydantic model annotated on the task body's `input_data` param."""
    hints = typing.get_type_hints(fn)
    params = inspect.signature(fn).parameters
    name = next(iter(params))
    model = hints.get(name)
    if model is None or not issubclass(model, BaseModel):
        raise TypeError(
            f"{fn.__name__} must annotate its first parameter with a pydantic model"
        )
    return model


def async_task(name: str, **task_options: Any):
    """Register an ``async def`` body as a Celery task named ``open_notebook.<name>``.

    Retry behaviour is expressed with Celery's own options, which map 1:1 onto
    the surreal-commands config they replace::

        max_attempts: 5           -> max_retries=4
        wait_strategy: exponential_jitter, wait_min/max: 1/60
                                  -> retry_backoff=1, retry_backoff_max=60,
                                     retry_jitter=True
        stop_on: [ValueError, ...] -> dont_autoretry_for=(ValueError, ...)
    """

    def decorator(fn: Callable):
        model = _input_model(fn)

        def run(self, input_data: Dict[str, Any], command_id: Optional[str] = None):
            # Claim the job (and honour a cancellation the broker never
            # delivered). Ignore leaves the task unstarted rather than failed.
            if not _claim_job(command_id):
                logger.info(f"Skipping {name} for {command_id}: job was cancelled")
                raise Ignore()

            payload = model(
                # A resubmitted payload may carry a stale context; the live
                # command_id kwarg always wins.
                **{k: v for k, v in input_data.items() if k != "execution_context"},
                execution_context=ExecutionContext(command_id=command_id),
            )
            result = asyncio.run(fn(payload))
            # mode="json" so datetimes survive both the Redis result backend
            # and the command row.
            return result.model_dump(mode="json") if isinstance(result, BaseModel) else result

        # Deliberately NOT functools.wraps: it would set __wrapped__ to the
        # async body, and Celery derives a task's call signature via
        # inspect.signature (which follows __wrapped__) — it would then reject
        # the command_id kwarg this wrapper adds.
        run.__name__ = fn.__name__
        run.__doc__ = fn.__doc__

        task = celery.task(name=f"{APP_NAME}.{name}", bind=True, **task_options)(run)
        # The in-process escape hatch (synchronous source processing) calls the
        # body directly instead of round-tripping the broker just to block.
        task.impl = fn
        return task

    return decorator


# ---------------------------------------------------------------------------
# Submission and status
# ---------------------------------------------------------------------------


async def submit_job(
    command: str, args: Dict[str, Any], *, app: str = APP_NAME
) -> str:
    """Enqueue a background job. Returns the ``command:<id>`` record id.

    The command row is created *before* the task is published: callers store
    that id on the source/episode they just made, and the UI starts polling it
    immediately — both need it to exist even if the worker picks the task up
    in the same millisecond.

    Dispatch goes through ``send_task`` (publish by name), so the API process
    does not need the task modules imported to submit work.
    """
    task_id = str(uuid4())
    created = await repo_create(
        COMMAND_TABLE,
        {
            "app": app,
            "command": command,
            "task_id": task_id,
            "input": args,
            "status": QUEUED,
            "attempt": 0,
            "progress": None,
            "result": None,
            "error_message": None,
        },
    )
    row = created[0] if isinstance(created, list) else created
    command_id = str(row["id"])

    try:
        celery.send_task(
            f"{app}.{command}",
            kwargs={"input_data": args, "command_id": command_id},
            task_id=task_id,
        )
    except Exception as e:
        # The row exists but nothing will ever run it — mark it failed rather
        # than leaving a job stuck in `queued` forever.
        logger.error(f"Failed to publish {app}.{command}: {e}")
        await _update_command(
            command_id,
            {"status": FAILED, "error_message": f"Failed to enqueue job: {e}"},
        )
        raise

    logger.info(f"Submitted {app}.{command} as {command_id} (task {task_id})")
    return command_id


async def get_job(command_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single command row, or None when it does not exist."""
    rows = await repo_query(
        "SELECT * FROM $id",
        {"id": ensure_record_id(command_id)},
    )
    return rows[0] if rows else None


async def list_jobs(
    *,
    active_only: bool = False,
    command: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List command rows, newest first."""
    where: List[str] = []
    vars: Dict[str, Any] = {"limit": max(1, min(limit, 500))}
    if active_only:
        where.append("status IN $active")
        vars["active"] = list(ACTIVE_STATUSES)
    if status:
        where.append("status = $status")
        vars["status"] = status
    if command:
        where.append("command = $command")
        vars["command"] = command
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return await repo_query(
        f"SELECT * FROM {COMMAND_TABLE}{clause} ORDER BY created DESC LIMIT $limit",
        vars,
    )


async def cancel_job(command_id: str) -> bool:
    """Revoke a queued job.

    Returns False when the job is already running or finished. Under the
    threads pool a running task cannot be terminated (there is no process to
    signal), so cancellation is honest about only covering the queue —
    see ADR-009.
    """
    row = await get_job(command_id)
    if not row:
        return False
    status = row.get("status")
    if status in TERMINAL_STATUSES or status == RUNNING:
        return False

    task_id = row.get("task_id")
    if task_id:
        celery.control.revoke(str(task_id))
    await _update_command(
        command_id,
        {"status": CANCELLED, "completed_at": datetime.now(timezone.utc)},
    )
    return True
