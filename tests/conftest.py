"""
Pytest configuration file.

This file ensures that the project root is in the Python path,
allowing tests to import from the api and open_notebook modules.
"""

import os
import sys
from pathlib import Path

# Ensure password auth is disabled for tests BEFORE any imports
# The PasswordAuthMiddleware skips auth when this env var is not set
# Set to empty string instead of deleting to prevent it from being reloaded
os.environ["OPEN_NOTEBOOK_PASSWORD"] = ""

# Load environment variables from .env file
# This must be done BEFORE any imports that depend on environment variables
from dotenv import load_dotenv

# Load .env file from project root
dotenv_path = Path(__file__).parent.parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)
    print(f"Loaded environment variables from {dotenv_path}")
else:
    print(f"Warning: .env file not found at {dotenv_path}")

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# --- Celery task helpers -----------------------------------------------------
#
# Shared by the retry-behaviour tests. They drive Celery's real autoretry
# wrapper (celery/app/autoretry.py) rather than re-implementing its rules, so
# they fail if the task options stop meaning what we think they mean.

from contextlib import contextmanager  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402


class RetryCalled(Exception):
    """Stand-in for the Retry that Celery's autoretry wrapper would raise."""


@contextmanager
def capture_retries(task):
    """Record every ``task.retry(...)`` the autoretry wrapper makes.

    Yields a list of the ``countdown`` values (the computed backoff delays);
    an empty list means the exception was treated as permanent.
    """
    countdowns: List[Optional[float]] = []

    def fake_retry(*_args: Any, **kwargs: Any) -> Exception:
        countdowns.append(kwargs.get("countdown"))
        # The wrapper does `raise task.retry(...)`, so returning is enough.
        return RetryCalled()

    original = task.retry
    task.retry = fake_retry
    task.push_request(retries=len(countdowns))
    try:
        yield countdowns
    finally:
        task.pop_request()
        task.retry = original


def run_task_expecting(task, payload: Dict[str, Any], countdowns: List) -> Exception:
    """Invoke a task body and return whatever came out of the autoretry wrapper."""
    try:
        task.run(payload)
    except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion
        return exc  # type: ignore[return-value]
    raise AssertionError("task body was expected to raise")
