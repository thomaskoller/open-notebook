"""Job state transitions must reach the `command` row.

Celery executes; SurrealDB records (ADR-009). The signal handlers below are the
only thing connecting the two, so if they stop writing, the UI shows every job
stuck in `queued` forever while the work actually runs -- the exact silent
failure mode this migration set out to remove.
"""

from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.celery_app import (
    CANCELLED,
    COMPLETED,
    FAILED,
    QUEUED,
    RETRYING,
    RUNNING,
    cancel_job,
    submit_job,
)


class FakeRequest:
    def __init__(self, command_id="command:abc", retries=0):
        self.kwargs = {"command_id": command_id}
        self.retries = retries


@pytest.fixture
def recorded(monkeypatch):
    """Capture _record_state calls instead of hitting the database."""
    from open_notebook import celery_app

    calls = []
    monkeypatch.setattr(
        celery_app,
        "_record_state",
        lambda command_id, **fields: calls.append((command_id, fields)),
    )
    return calls


class TestSubmission:
    @pytest.mark.asyncio
    async def test_row_is_created_before_the_task_is_published(self):
        """Callers store the returned id on the source/episode they just made,
        and the UI starts polling it immediately - both need the row to exist
        even if the worker picks the task up in the same millisecond."""
        order = []

        async def fake_create(table, data):
            order.append(("create", table, data["status"]))
            return [{"id": "command:new1"}]

        with (
            patch(
                "open_notebook.celery_app.repo_create",
                new=AsyncMock(side_effect=fake_create),
            ),
            patch("open_notebook.celery_app.celery.send_task") as send,
        ):
            send.side_effect = lambda *a, **kw: order.append(("send", a, kw))
            command_id = await submit_job("embed_note", {"note_id": "note:1"})

        assert command_id == "command:new1"
        assert [step[0] for step in order] == ["create", "send"]
        assert order[0][2] == QUEUED

    @pytest.mark.asyncio
    async def test_task_carries_the_command_id_and_row_carries_the_task_id(self):
        """The two ids are the join between the command row and Flower."""
        created = {}

        async def fake_create(table, data):
            created.update(data)
            return [{"id": "command:new2"}]

        with (
            patch(
                "open_notebook.celery_app.repo_create",
                new=AsyncMock(side_effect=fake_create),
            ),
            patch("open_notebook.celery_app.celery.send_task") as send,
        ):
            await submit_job("embed_source", {"source_id": "source:1"})

        kwargs = send.call_args.kwargs
        assert kwargs["kwargs"]["command_id"] == "command:new2"
        assert kwargs["kwargs"]["input_data"] == {"source_id": "source:1"}
        assert kwargs["task_id"] == created["task_id"]
        assert created["command"] == "embed_source"

    @pytest.mark.asyncio
    async def test_publish_failure_marks_the_row_failed(self):
        """Otherwise the row sits in `queued` forever with nothing to run it."""
        updates = {}

        with (
            patch(
                "open_notebook.celery_app.repo_create",
                new=AsyncMock(return_value=[{"id": "command:new3"}]),
            ),
            patch(
                "open_notebook.celery_app._update_command",
                new=AsyncMock(side_effect=lambda cid, fields: updates.update(fields)),
            ),
            patch(
                "open_notebook.celery_app.celery.send_task",
                side_effect=RuntimeError("broker down"),
            ),
        ):
            with pytest.raises(RuntimeError):
                await submit_job("embed_note", {"note_id": "note:1"})

        assert updates["status"] == FAILED
        assert "broker down" in updates["error_message"]


class TestSignalHandlers:
    @pytest.mark.asyncio
    async def test_claim_marks_running_and_skips_a_cancelled_job(self):
        """A revoke broadcast only reaches workers that are up at the time, so
        the worker re-checks the command row before running anything."""
        from open_notebook import celery_app

        captured = {}

        async def fake_query(query, vars):
            captured["query"] = query
            captured["vars"] = vars
            return [{"id": "command:x"}] if vars["running"] == RUNNING else []

        with patch(
            "open_notebook.celery_app.repo_query", new=AsyncMock(side_effect=fake_query)
        ):
            assert await celery_app._claim("command:x") is True

        # One conditional UPDATE, so the check and the claim cannot interleave.
        assert "WHERE status != $cancelled" in captured["query"]
        assert captured["vars"]["cancelled"] == CANCELLED

        # No row comes back when the row is already cancelled.
        with patch(
            "open_notebook.celery_app.repo_query", new=AsyncMock(return_value=[])
        ):
            assert await celery_app._claim("command:x") is False

    def test_a_failed_claim_still_runs_the_job(self):
        """Losing a status write costs visibility, not the user's work."""
        from open_notebook import celery_app

        def explode(coro):
            coro.close()
            raise RuntimeError("db unreachable")

        with patch("open_notebook.celery_app.asyncio.run", side_effect=explode):
            assert celery_app._claim_job("command:x") is True

    def test_jobs_without_a_command_id_are_not_claimed(self):
        from open_notebook import celery_app

        with patch("open_notebook.celery_app.asyncio.run") as run:
            assert celery_app._claim_job(None) is True
        run.assert_not_called()

    def test_success_records_result(self, recorded):
        from open_notebook import celery_app

        sender = type("S", (), {"request": FakeRequest("command:y")})()
        celery_app._on_success(sender=sender, result={"success": True})

        command_id, fields = recorded[0]
        assert command_id == "command:y"
        assert fields["status"] == COMPLETED
        assert fields["result"] == {"success": True}
        # A retried-then-succeeded job must not keep its earlier error text.
        assert fields["error_message"] is None

    def test_failure_records_the_message(self, recorded):
        from open_notebook import celery_app

        celery_app._on_failure(
            task_id="t2",
            exception=ValueError("source not found"),
            kwargs={"command_id": "command:z"},
        )

        command_id, fields = recorded[0]
        assert fields["status"] == FAILED
        assert fields["error_message"] == "source not found"

    def test_revoked_marks_cancelled(self, recorded):
        from open_notebook import celery_app

        celery_app._on_revoked(request=FakeRequest("command:w"))

        assert recorded[0][1]["status"] == CANCELLED

    def test_handlers_are_noops_without_a_command_id(self):
        """Tasks published outside submit_job (e.g. `celery call`) must not
        blow up the worker."""
        from open_notebook import celery_app

        with patch("open_notebook.celery_app.asyncio.run") as run:
            celery_app._record_state(None, status=RUNNING)
        run.assert_not_called()

    def test_a_failed_status_write_never_fails_the_job(self):
        """Losing a status write costs visibility, not the user's work."""
        from open_notebook import celery_app

        # Close the coroutine the stubbed asyncio.run never gets to run, so
        # the test doesn't leak a "never awaited" warning.
        def explode(coro):
            coro.close()
            raise RuntimeError("db unreachable")

        with patch("open_notebook.celery_app.asyncio.run", side_effect=explode):
            celery_app._record_state("command:q", status=RUNNING)  # must not raise


class TestCancellation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [RUNNING, COMPLETED, FAILED, CANCELLED])
    async def test_refuses_anything_already_started_or_finished(self, status):
        """The threads pool has no process to signal, so a started task always
        runs to completion - say so rather than pretend (ADR-009)."""
        with patch(
            "open_notebook.celery_app.get_job",
            new=AsyncMock(return_value={"status": status, "task_id": "t"}),
        ):
            assert await cancel_job("command:a") is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [QUEUED, RETRYING])
    async def test_revokes_work_that_has_not_started(self, status):
        with (
            patch(
                "open_notebook.celery_app.get_job",
                new=AsyncMock(return_value={"status": status, "task_id": "task-1"}),
            ),
            patch("open_notebook.celery_app._update_command", new=AsyncMock()) as upd,
            patch("open_notebook.celery_app.celery.control.revoke") as revoke,
        ):
            assert await cancel_job("command:b") is True

        revoke.assert_called_once_with("task-1")
        assert upd.call_args.args[1]["status"] == CANCELLED

    @pytest.mark.asyncio
    async def test_unknown_job_is_not_cancellable(self):
        with patch("open_notebook.celery_app.get_job", new=AsyncMock(return_value=None)):
            assert await cancel_job("command:missing") is False
