"""Context-length rejections must fail immediately instead of being retried.

Regression coverage for #1231. A provider's context-length 400 is deterministic:
the same oversized payload is resent on every attempt, so retrying can never
succeed. Before the fix it surfaced as `ExternalServiceError` -- the same class
used for provider 5xx -- so the retry blocklist could not tell the two apart and
retried both, re-running the whole source pipeline for ~25 minutes.

Two properties are pinned here:

1. `classify_error()` maps a context-length message to `ContextLengthExceededError`
   (guards the rule and its ordering in `_CLASSIFICATION_RULES`).
2. The registered tasks treat it as permanent, while a transient error still
   gets retried with backoff (guards `dont_autoretry_for`, and guards against
   over-correcting into "never retry anything").

The retry assertions drive Celery's real autoretry wrapper, so they break if the
task options stop meaning what we think they mean.
"""

import pytest
from conftest import RetryCalled, capture_retries, run_task_expecting

import commands  # noqa: F401  -- import registers the tasks
from open_notebook.celery_app import celery
from open_notebook.exceptions import (
    ContextLengthExceededError,
    ExternalServiceError,
)
from open_notebook.utils.error_classifier import classify_error

# The message OpenRouter returned in #1231, as the OpenAI SDK surfaces it.
OPENROUTER_CONTEXT_LENGTH_400 = (
    "Error code: 400 - {'error': {'message': \"This endpoint's maximum context "
    "length is 262144 tokens. However, you requested about 403309 tokens "
    "(395117 of text input, 8192 in the output).\", 'code': 400}}"
)

# Tasks whose retry config must treat context-length errors as permanent.
RETRY_TASKS = [
    "open_notebook.process_source",
    "open_notebook.run_transformation",
]


def _task(task_name: str):
    task = celery.tasks.get(task_name)
    assert task is not None, f"{task_name} is not registered"
    return task


class TestClassification:
    def test_openrouter_400_is_context_length_exceeded(self):
        exc_class, message = classify_error(Exception(OPENROUTER_CONTEXT_LENGTH_400))

        assert exc_class is ContextLengthExceededError
        assert message  # user-facing text, not empty

    def test_still_an_external_service_error(self):
        """Subclassing is load-bearing: it keeps the existing 502 handler."""
        assert issubclass(ContextLengthExceededError, ExternalServiceError)

    def test_provider_5xx_stays_retryable(self):
        """The regression guard -- 5xx must NOT be swept up as permanent."""
        exc_class, _ = classify_error(
            Exception("Error code: 503 - service unavailable, provider overloaded")
        )

        assert exc_class is ExternalServiceError
        assert not issubclass(exc_class, ContextLengthExceededError)


@pytest.mark.parametrize("task_name", RETRY_TASKS)
class TestRetryBehaviour:
    def test_dont_autoretry_for_lists_context_length(self, task_name):
        assert ContextLengthExceededError in _task(task_name).dont_autoretry_for

    def test_context_length_is_never_retried(self, task_name, monkeypatch):
        task = _task(task_name)
        monkeypatch.setattr(
            task,
            "_orig_run",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                ContextLengthExceededError("Content too large")
            ),
        )

        with capture_retries(task) as countdowns:
            raised = run_task_expecting(task, {}, countdowns)

        assert isinstance(raised, ContextLengthExceededError), (
            f"{task_name} did not let a deterministic context-length failure "
            f"through; it must fail on the first attempt (#1231)"
        )
        assert countdowns == [], (
            f"{task_name} scheduled {len(countdowns)} retry(s) for a "
            f"context-length failure"
        )

    def test_transient_error_still_retries(self, task_name, monkeypatch):
        """Proves the fix narrows retries rather than disabling them."""
        task = _task(task_name)
        monkeypatch.setattr(
            task,
            "_orig_run",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                RuntimeError("Failed to commit transaction due to a conflict")
            ),
        )

        with capture_retries(task) as countdowns:
            raised = run_task_expecting(task, {}, countdowns)

        assert isinstance(raised, RetryCalled)
        assert len(countdowns) == 1
        # Backoff is bounded by the task's own ceiling.
        assert 0 <= countdowns[0] <= task.retry_backoff_max

    def test_retry_budget_preserved(self, task_name):
        """Total attempts must match what surreal-commands used to allow."""
        expected = {
            "open_notebook.process_source": 14,  # was max_attempts: 15
            "open_notebook.run_transformation": 4,  # was max_attempts: 5
        }
        assert _task(task_name).max_retries == expected[task_name]
