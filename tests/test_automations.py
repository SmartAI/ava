"""Schedule golden cases and durable dispatch through real backend HTTP sessions."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ava.app.web.automations import (
    AutomationConflict,
    AutomationDefinition,
    AutomationStore,
    Schedule,
)
from ava.app.web.server import create_app
from tests.test_web import _running_client
from tests.test_web import scripted as scripted  # noqa: F401


def definition(start="2026-01-01T09:00:00", *, count=5, cadence="days", **kwargs):
    return AutomationDefinition(name="Project brief", project_id="workspace", prompt="Review recent work.",
                                schedule=Schedule(start_local=start, timezone="UTC", count=count, cadence=cadence), **kwargs)


@pytest.mark.parametrize(("start", "zone", "cadence", "expected"), [
    ("2026-03-07T02:30", "America/Vancouver", "days", ["2026-03-07T10:30:00+00:00", "2026-03-08T10:00:00+00:00", "2026-03-09T09:30:00+00:00"]),
    ("2020-10-31T01:30", "America/Los_Angeles", "days", ["2020-10-31T08:30:00+00:00", "2020-11-01T08:30:00+00:00", "2020-11-02T09:30:00+00:00"]),
    ("2026-03-08T01:30", "America/Vancouver", "hours", ["2026-03-08T09:30:00+00:00", "2026-03-08T10:30:00+00:00", "2026-03-08T11:30:00+00:00"]),
    ("2026-04-04T02:15", "Australia/Lord_Howe", "days", ["2026-04-03T15:15:00+00:00", "2026-04-04T15:45:00+00:00", "2026-04-05T15:45:00+00:00"]),
])
def test_schedule_preserves_wall_time_or_elapsed_intervals(start, zone, cadence, expected):
    schedule = Schedule(start_local=start, timezone=zone, cadence=cadence, count=3)
    assert [schedule.occurrence(i).isoformat() for i in range(3)] == expected
    assert schedule.due_through(0, datetime.fromisoformat(expected[-1])) == 2
    assert schedule.due_through(0, datetime.fromisoformat(expected[0]) - timedelta(microseconds=1)) == -1
    for invalid in ({"timezone": "invalid-zone"}, {"count": 0}, {"count": True}, {"start_local": "2026-01-01T09:00Z"}):
        with pytest.raises((ValidationError, ValueError)):
            Schedule.model_validate({**schedule.model_dump(), **invalid})


def test_schedule_claims_coalesce_without_overlap_and_survive_reopen(tmp_path):
    path = tmp_path / "automations.sqlite3"
    first, second = AutomationStore(path), AutomationStore(path)
    now = datetime(2026, 1, 1, 9, tzinfo=UTC)
    task = first.save(definition(), now)
    try:
        with ThreadPoolExecutor(2) as pool:
            claimed = list(pool.map(lambda store: store.claim_due(now + timedelta(days=2)), (first, second)))
        runs = [run for batch in claimed for run in batch]
        assert len(runs) == 1 and runs[0]["occurrence"] == 2
        progress = first.get(task["id"])
        assert progress["consumed"] == 3 and progress["skipped"] == 2 and progress["remaining"] == 2
        assert not first.claim_due(now + timedelta(days=3))
        assert first.get(task["id"])["skipped"] == 3
        first.update_run(runs[0]["id"], status="completed", session_id="session-one", now=now + timedelta(days=3))
    finally:
        first.close()
        second.close()
    restored = AutomationStore(path)
    try:
        run = restored.claim_due(now + timedelta(days=4))[0]
        assert run["occurrence"] == 4
        assert restored.get(task["id"])["remaining"] == 0
        assert not restored.claim_due(now + timedelta(days=10))
        assert len(restored.history(task["id"])["runs"]) == 2
        assert "prompt" not in restored.list_tasks()[0]
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        restored.close()


def test_pause_manual_run_edit_conflicts_and_removal_keep_history(tmp_path):
    store = AutomationStore(tmp_path / "automations.sqlite3")
    now = datetime(2026, 1, 1, 8, tzinfo=UTC)
    task = store.save(definition(), now)
    identity = task["id"]
    try:
        request_id = uuid4().hex
        run, created = store.manual_run(identity, request_id, now)
        assert created and store.manual_run(identity, request_id, now) == (run, False)
        assert store.get(identity)["remaining"] == 5
        with pytest.raises(AutomationConflict):
            store.manual_run(identity, uuid4().hex, now)
        store.set_enabled(identity, False, now)
        assert not store.claim_due(now + timedelta(days=2, hours=2))
        assert store.active_runs()[0]["id"] == run["id"]
        resumed = store.set_enabled(identity, True, now + timedelta(days=2, hours=2))
        assert resumed["remaining"] == 2 and resumed["skipped"] == 3
        with pytest.raises(AutomationConflict):
            store.save(definition(), now, identity, task["version"])
        edited = definition()
        edited.prompt = "Revised future work"
        updated = store.save(edited, now, identity, resumed["version"])
        assert updated["remaining"] == 2
        assert "Review recent work" in store.history(identity)["runs"][0]["snapshot"]
        store.update_run(run["id"], status="completed", now=now)
        for _ in range(24):
            older, _ = store.manual_run(identity, uuid4().hex, now)
            store.update_run(older["id"], status="completed", now=now)
        newest = store.history(identity)
        older_page = store.history(identity, newest["runs"][-1]["cursor"])
        assert len(newest["runs"]) == 20 and newest["has_more"]
        assert len(older_page["runs"]) == 5 and not older_page["has_more"]
        assert len({row["id"] for row in newest["runs"] + older_page["runs"]}) == 25
        store.remove(identity, now)
        assert not store.list_tasks() and not store.claim_due(now + timedelta(days=5))
        assert store.get_run(run["id"])["id"] == run["id"]
    finally:
        store.close()


async def wait_for_runs(client, identity, count):
    async with asyncio.timeout(8):
        while True:
            response = await client.get(f"/api/automations/{identity}")
            assert response.status_code == 200, response.text
            detail = response.json()
            if len(detail["runs"]) >= count and all(run["status"] not in ("starting", "running") for run in detail["runs"]):
                return detail
            await asyncio.sleep(0.01)


async def test_automation_dispatches_durable_sessions_and_recovers_without_replay(home, project, scripted):
    current = datetime(2026, 1, 1, 8, 59, tzinfo=UTC)
    app = create_app(project)
    engine = app.state.automations
    engine.now = lambda: current
    async with _running_client(app) as client:
        task = definition(count=3, cadence="minutes")
        preview = (await client.post("/api/automations/preview", json=task.schedule.model_dump())).json()
        assert preview["occurrences"][0]["at"] == "2026-01-01T09:00:00+00:00"
        creation = {**task.model_dump(), "request_id": uuid4().hex}
        created = await client.post("/api/automations", json=creation)
        assert created.status_code == 201, created.text
        identity = created.json()["id"]
        assert (await client.post("/api/automations", json=creation)).json()["id"] == identity
        assert len((await client.get("/api/automations")).json()["automations"]) == 1
        assert not (await client.get(f"/api/automations/{identity}")).json()["runs"]
        current += timedelta(minutes=1)
        engine.wake.set()
        first = await wait_for_runs(client, identity, 1)
        assert first["runs"][0]["status"] == "completed"
        assert first["runs"][0]["scheduled_for"] == current.timestamp()
        chat_id = first["runs"][0]["chat_id"]
        assert chat_id
        chats = (await client.get("/api/projects")).json()["projects"][0]["chats"]
        assert chats[0]["automation_id"] == identity and chats[0]["automation_run"] == first["runs"][0]["id"]
        assert chats[0]["completion_seq"] > chats[0]["reviewed_through"]
        current += timedelta(minutes=3)
        engine.wake.set()
        finished = await wait_for_runs(client, identity, 2)
        assert finished["remaining"] == 0 and finished["skipped"] == 1
        assert len({run["chat_id"] for run in finished["runs"]}) == 2
        assert [run["occurrence"] for run in finished["runs"]] == [2, 0]
        manual_id = uuid4().hex
        first_request = await client.post(f"/api/automations/{identity}/run", json={"request_id": manual_id})
        assert first_request.status_code == 202
        assert (await client.post(f"/api/automations/{identity}/run", json={"request_id": manual_id})).json()["id"] == manual_id
        result = await wait_for_runs(client, identity, 3)
        assert result["remaining"] == 0
        assert sum(provider.calls for provider in scripted) == 3
        expected_chats = {run["chat_id"] for run in result["runs"]}
    # Simulate the durable boundaries of a crash: a completed session whose final
    # ledger update was lost, and a claimed occurrence with no confirmed session.
    store = AutomationStore(home / "automations.sqlite3")
    completed_run = result["runs"][0]["id"]
    store.update_run(completed_run, status="running", now=current)
    pending_task = store.save(definition(start="2026-02-01T09:00"), current)
    uncertain, _ = store.manual_run(pending_task["id"], uuid4().hex, current)
    store.close()
    calls = sum(provider.calls for provider in scripted)
    restarted = create_app(project)
    restarted.state.automations.now = lambda: current
    async with _running_client(restarted) as client:
        restored = (await client.get(f"/api/automations/{identity}")).json()
        assert {run["chat_id"] for run in restored["runs"]} == expected_chats
        assert all(run["status"] == "completed" for run in restored["runs"])
        interrupted = (await client.get(f"/api/automations/{pending_task['id']}")).json()["runs"][0]
        assert interrupted["id"] == uncertain["id"] and interrupted["status"] == "interrupted"
        assert not interrupted["chat_id"] and interrupted["error"]
        assert sum(provider.calls for provider in scripted) == calls
        assert (await client.delete(f"/api/automations/{identity}")).status_code == 200
        assert (await client.get(f"/api/chats/{chat_id}")).status_code == 200


async def test_automation_worktrees_retain_failures_and_separate_each_execution(home, project, scripted):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(project), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Ava fixture")
    git("config", "user.email", "ava@example.invalid")
    git("config", "commit.gpgsign", "false")
    (project / "answer.txt").write_text("committed")
    git("add", ".")
    git("commit", "-qm", "initial")
    (project / "answer.txt").write_text("uncommitted")
    async with _running_client(create_app(project)) as client:
        task = definition(start="2100-01-01T09:00", workspace="worktree", base_ref="missing-branch")
        created = (await client.post("/api/automations", json=task.model_dump())).json()
        identity = created["id"]
        await client.post(f"/api/automations/{identity}/run", json={"request_id": uuid4().hex})
        failed = await wait_for_runs(client, identity, 1)
        assert failed["runs"][0]["status"] == "failed" and failed["runs"][0]["error"]
        assert not failed["runs"][0]["chat_id"]
        assert sum(provider.calls for provider in scripted) == 0
        fixed = {**task.model_dump(), "base_ref": "HEAD", "version": failed["version"]}
        assert (await client.put(f"/api/automations/{identity}", json=fixed)).status_code == 200
        for count in (2, 3):
            await client.post(f"/api/automations/{identity}/run", json={"request_id": uuid4().hex})
            result = await wait_for_runs(client, identity, count)
            assert result["runs"][0]["status"] == "completed"
        chats = (await client.get("/api/projects")).json()["projects"][0]["chats"]
        assert len(chats) == 2 and len({chat["cwd"] for chat in chats}) == 2
        assert all(chat["automation_id"] == identity for chat in chats)
        for chat in chats:
            directory = Path(chat["cwd"])
            assert directory.is_relative_to(home / "worktrees")
            assert (directory / "answer.txt").read_text() == "committed"
        assert (project / "answer.txt").read_text() == "uncommitted"
        assert git("branch", "--show-current") == "main"
        assert result["remaining"] == 5


async def test_scheduler_errors_reach_desktop_heartbeat_and_recover(home, project, monkeypatch):
    app = create_app(project)
    engine = app.state.automations
    async with _running_client(app) as client:
        revision = (await client.get("/api/system")).json()["automation_revision"]
        original = engine.store.claim_due

        def unavailable(*_):
            raise sqlite3.OperationalError("fixture storage unavailable")

        monkeypatch.setattr(engine.store, "claim_due", unavailable)
        engine.wake.set()
        async with asyncio.timeout(3):
            while not engine.error:
                await asyncio.sleep(0.01)
        status = (await client.get("/api/system")).json()
        assert status["automation_revision"] == revision
        assert status.get("automation_error") == engine.error, "A cached desktop must learn about scheduler failures even without a database revision"
        assert (await client.get("/api/automations")).json()["error"] == engine.error
        monkeypatch.setattr(engine.store, "claim_due", original)
        engine.wake.set()
        async with asyncio.timeout(3):
            while engine.error:
                await asyncio.sleep(0.01)
        assert (await client.get("/api/system")).json()["automation_error"] == ""
