"""An abandoned job must stop counting as "in progress".

Every terminal transition is written by a Celery signal inside the worker, so a
worker killed mid-task (container restart, OOM) leaves its row `running` and a
queued message lost with the broker leaves it `queued` -- forever. The jobs
indicator then reports work that will never finish (observed: "25 in progress"
with nothing running).
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.command_service import _serialize
from open_notebook.celery_app import (
    ACTIVE_STATUSES,
    STALE_JOB_MINUTES,
    TERMINAL_STATUSES,
    effective_status,
)


def _row(status, *, age_minutes=0, field="updated"):
    when = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {"id": "command:x", "status": status, field: when}


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
def test_fresh_active_job_is_untouched(status):
    assert effective_status(_row(status, age_minutes=1)) == status


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
def test_abandoned_active_job_reads_as_unknown(status):
    assert effective_status(_row(status, age_minutes=STALE_JOB_MINUTES + 1)) == "unknown"


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_status_is_never_rewritten(status):
    """A completed job from last year is still completed, not unknown."""
    assert effective_status(_row(status, age_minutes=60 * 24 * 365)) == status


@pytest.mark.parametrize("field", ["updated", "started_at", "created"])
def test_any_timestamp_counts_as_a_sign_of_life(field):
    """_claim writes started_at without touching `updated`, and a job that has
    never reported progress only has `created`."""
    assert effective_status(_row("running", age_minutes=1, field=field)) == "running"


def test_newest_timestamp_wins():
    """An old row that just reported progress is active, not abandoned."""
    now = datetime.now(timezone.utc)
    row = {
        "status": "running",
        "created": now - timedelta(hours=5),
        "started_at": now - timedelta(hours=5),
        "updated": now - timedelta(seconds=30),
    }
    assert effective_status(row) == "running"


def test_row_with_no_timestamps_is_left_alone():
    assert effective_status({"status": "running"}) == "running"


def test_missing_status_is_unknown():
    assert effective_status({}) == "unknown"


def test_serialized_job_reports_the_effective_status():
    """The drawer and the sidebar indicator both read this shape."""
    stale = _serialize(_row("running", age_minutes=STALE_JOB_MINUTES + 5))
    assert stale["status"] == "unknown"
    # `unknown` is not in the frontend's ACTIVE_JOB_STATUSES, so it drops out
    # of the count and stops the 2s poll.
    assert stale["status"] not in ACTIVE_STATUSES
