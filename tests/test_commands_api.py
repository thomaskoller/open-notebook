"""The jobs endpoints the UI depends on.

`GET /api/commands/jobs` and `DELETE /api/commands/jobs/{id}` used to be stubs
that returned `[]` and `True` without doing anything, so there was no way to see
or stop in-flight work. These pin the real behaviour.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


def make_row(**overrides):
    row = {
        "id": "command:abc",
        "status": "running",
        "command": "embed_source",
        "task_id": "task-1",
        "result": None,
        "error_message": None,
        "progress": {"message": "extracting"},
        "attempt": 0,
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:01Z",
    }
    row.update(overrides)
    return row


class TestListJobs:
    def test_returns_jobs_with_progress_and_task_id(self, client):
        with patch(
            "api.command_service.list_jobs",
            new=AsyncMock(return_value=[make_row()]),
        ):
            response = client.get("/api/commands/jobs")

        assert response.status_code == 200
        [job] = response.json()
        assert job["job_id"] == "command:abc"
        assert job["command"] == "embed_source"
        # task_id is the handle for looking the job up in Flower.
        assert job["task_id"] == "task-1"
        assert job["progress"] == {"message": "extracting"}

    def test_active_flag_reaches_the_query(self, client):
        """This is the drawer's request: everything in flight, one round trip."""
        with patch("api.command_service.list_jobs", new=AsyncMock(return_value=[])) as q:
            response = client.get("/api/commands/jobs", params={"active": "true"})

        assert response.status_code == 200
        assert q.call_args.kwargs["active_only"] is True

    def test_filters_are_passed_through(self, client):
        with patch("api.command_service.list_jobs", new=AsyncMock(return_value=[])) as q:
            client.get(
                "/api/commands/jobs",
                params={"command_filter": "embed_note", "status_filter": "failed"},
            )

        assert q.call_args.kwargs["command"] == "embed_note"
        assert q.call_args.kwargs["status"] == "failed"

    def test_limit_is_bounded(self, client):
        response = client.get("/api/commands/jobs", params={"limit": 10_000})
        assert response.status_code == 422


class TestJobStatus:
    def test_missing_job_reports_unknown_rather_than_500(self, client):
        with patch("api.command_service.get_job", new=AsyncMock(return_value=None)):
            response = client.get("/api/commands/jobs/command:gone")

        assert response.status_code == 200
        assert response.json()["status"] == "unknown"


class TestCancelJob:
    def test_cancels_a_queued_job(self, client):
        with patch("api.command_service.cancel_job", new=AsyncMock(return_value=True)):
            response = client.delete("/api/commands/jobs/command:abc")

        assert response.status_code == 200
        assert response.json() == {"job_id": "command:abc", "cancelled": True}

    def test_running_job_is_a_conflict_not_a_silent_success(self, client):
        """The old stub always claimed success; a running task cannot be
        stopped under the threads pool, and the UI needs to know."""
        with patch("api.command_service.cancel_job", new=AsyncMock(return_value=False)):
            response = client.delete("/api/commands/jobs/command:abc")

        assert response.status_code == 409
        assert "already started" in response.json()["detail"]
