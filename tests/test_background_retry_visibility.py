"""Background LLM and embedding retries must be visible in normal worker logs.

A silently retrying provider call is indistinguishable from a hung job. Celery
logs retries itself, but at its own level and without the job's identity, so
`open_notebook.celery_app._on_retry` emits the line the operator actually reads
-- and records the attempt on the command row so the jobs drawer can show it.
"""

import pytest
from loguru import logger

import commands  # noqa: F401 -- import registers the tasks
from open_notebook.celery_app import RETRYING, celery

# Tasks whose retries must be loud. process_source is deliberately absent: it
# retries on SurrealDB transaction conflicts during deep queues, which are
# expected and would drown the log (hence retry_log_level="debug").
VISIBLE_RETRY_TASKS = [
    "open_notebook.run_transformation",
    "open_notebook.embed_note",
    "open_notebook.embed_insight",
    "open_notebook.embed_source",
    "open_notebook.create_insight",
]


class FakeRequest:
    def __init__(self, command_id="command:abc", retries=0):
        self.kwargs = {"command_id": command_id}
        self.retries = retries


def _task(name):
    task = celery.tasks.get(name)
    assert task is not None, f"{name} is not registered"
    return task


@pytest.mark.parametrize("task_name", VISIBLE_RETRY_TASKS)
def test_background_retry_is_logged_at_warning(task_name):
    assert _task(task_name).retry_log_level == "warning"


def test_noisy_transaction_conflicts_stay_at_debug():
    """Guards the deliberate exception to the rule above."""
    assert _task("open_notebook.process_source").retry_log_level == "debug"


def test_retry_handler_logs_attempt_and_error(monkeypatch):
    from open_notebook import celery_app

    recorded: dict = {}
    monkeypatch.setattr(
        celery_app,
        "_record_state",
        lambda command_id, **fields: recorded.update(command_id=command_id, **fields),
    )

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        celery_app._on_retry(
            sender=_task("open_notebook.run_transformation"),
            request=FakeRequest(retries=1),
            reason=ConnectionError("provider temporarily unavailable"),
        )
    finally:
        logger.remove(sink_id)

    assert len(messages) == 1
    assert "[Retry] Attempt 2" in messages[0]
    assert "run_transformation" in messages[0]
    assert "ConnectionError" in messages[0]
    assert "provider temporarily unavailable" in messages[0]

    # The same transition must reach the command row, or the UI shows a job
    # sitting in `running` while it is actually backing off.
    assert recorded["command_id"] == "command:abc"
    assert recorded["status"] == RETRYING
    assert recorded["attempt"] == 2
    assert "provider temporarily unavailable" in recorded["error_message"]
