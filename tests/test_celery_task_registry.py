"""The Celery task table must match the surreal-commands config it replaced.

The migration (ADR-009) translated each `@command(retry={...})` into Celery task
options. Retry budgets are load-bearing -- process_source's 15 attempts exist to
outlast SurrealDB transaction conflicts on deep queues, and generate_podcast's
zero exist so a retry cannot produce a duplicate episode (and a second round of
TTS spend). This pins the whole table so a future edit to one task can't quietly
change another.
"""

import pytest

import commands  # noqa: F401 -- import registers the tasks
from open_notebook.celery_app import celery
from open_notebook.exceptions import ConfigurationError, ContextLengthExceededError

PERMANENT = (ValueError, ConfigurationError, ContextLengthExceededError)

# task name -> (max_retries, retry_backoff_max, dont_autoretry_for)
#
# max_retries is (old max_attempts - 1): Celery counts retries, surreal-commands
# counted total attempts.
EXPECTED = {
    "process_source": (14, 120, PERMANENT),
    "run_transformation": (4, 60, PERMANENT),
    "embed_note": (4, 60, PERMANENT),
    "embed_insight": (4, 60, PERMANENT),
    "embed_source": (4, 60, PERMANENT),
    "create_insight": (4, 60, PERMANENT),
    "rebuild_embeddings": (0, None, ()),  # coordinator: fans out, never retried
    "generate_podcast": (0, None, ()),  # retry would duplicate the episode
}


def test_exactly_these_tasks_are_registered():
    """A new task must be added here consciously, not discovered in production."""
    registered = {
        name.removeprefix("open_notebook.")
        for name in celery.tasks
        if name.startswith("open_notebook.")
    }
    assert registered == set(EXPECTED)


@pytest.mark.parametrize("short_name", sorted(EXPECTED))
def test_retry_options_match_the_replaced_config(short_name):
    max_retries, backoff_max, permanent = EXPECTED[short_name]
    task = celery.tasks[f"open_notebook.{short_name}"]

    assert task.max_retries == max_retries
    assert tuple(getattr(task, "dont_autoretry_for", ()) or ()) == permanent

    if backoff_max is None:
        # Nothing auto-retries, so the backoff ceiling is irrelevant.
        assert not getattr(task, "autoretry_for", ())
    else:
        assert task.retry_backoff_max == backoff_max
        assert task.retry_jitter is True


@pytest.mark.parametrize("short_name", sorted(EXPECTED))
def test_task_exposes_its_async_body(short_name):
    """`.impl` is the in-process escape hatch (synchronous source processing)."""
    import inspect

    task = celery.tasks[f"open_notebook.{short_name}"]
    assert inspect.iscoroutinefunction(task.impl)
