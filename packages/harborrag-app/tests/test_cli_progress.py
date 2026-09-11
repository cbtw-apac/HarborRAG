"""One snapshot schema feeds the inline renderer for direct and Temporal runs."""

from __future__ import annotations

import asyncio
import json
from io import StringIO

import pytest
from rich.console import Console

from harborrag_app.cli.progress import (
    ProgressSnapshot,
    ProgressUnavailable,
    StatusSource,
    TaskSource,
)
from harborrag_app.cli.progress_view import LiveProgress, render_snapshot
from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.errors import IngestionNotFoundError


def test_status_payload_uses_the_temporal_headline() -> None:
    snapshot = ProgressSnapshot.from_status_payload(
        {
            "status": {"run_id": "r1", "status": "RUNNING"},
            "execution_status": "terminated",
            "progress": {"discovered": 4, "processed": 2},
            "failed_artifacts": ["a.pdf"],
        }
    )
    assert snapshot.run_id == "r1"
    assert snapshot.status == "failed"
    assert snapshot.terminal is True
    assert snapshot.failed_artifacts == ("a.pdf",)


def test_task_payload_maps_public_names_to_run_vocabulary() -> None:
    snapshot = ProgressSnapshot.from_task_payload(
        {
            "task_id": "t1",
            "status": "PARTIAL",
            "stage": "COMPLETED",
            "progress": {"discovered": 3, "processed": 3, "failed": 1},
            "message": "done",
        }
    )
    assert snapshot.status == "completed"
    assert snapshot.terminal is True
    assert snapshot.as_dict()["progress"]["failed"] == 1


class _Service:
    def __init__(self, responses):
        self.responses = list(responses)

    async def ingestion_status(self, run_id):
        return self.responses.pop(0)


def test_status_source_raises_when_the_service_fails() -> None:
    source = StatusSource(_Service([AppResponse(False, error="boom")]), "r1")
    with pytest.raises(ProgressUnavailable, match="boom"):
        asyncio.run(source.snapshot())


class _Reader:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    async def get_task(self, task_id):
        item = self.payloads.pop(0)
        if item is None:
            raise IngestionNotFoundError("Ingestion task was not found.")
        return item


def test_task_source_returns_none_until_the_task_is_registered() -> None:
    reader = _Reader([None, {"task_id": "t", "status": "RUNNING", "progress": {}}])
    source = TaskSource(reader, "t")
    assert asyncio.run(source.snapshot()) is None
    second = asyncio.run(source.snapshot())
    assert second is not None and second.status == "running"


def test_render_snapshot_includes_counts_and_failures() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    snapshot = ProgressSnapshot(
        "r1",
        "running",
        {"discovered": 10, "processed": 4, "succeeded": 3, "failed": 1},
        failed_artifacts=("docs/x.pdf",),
    )
    console.print(render_snapshot(snapshot, label="workspace", elapsed=65.0))
    text = output.getvalue()
    assert "workspace" in text and "RUNNING" in text and "01:05" in text
    assert "4/10" in text and "failed 1" in text and "docs/x.pdf" in text


class _Scripted:
    """Replays scripted snapshots, repeating the last one, under a poll budget.

    `follow` runs with `interval=0`, so a regression in the terminal check or the
    `until.done()` branch would spin forever and hang CI instead of failing.
    """

    def __init__(self, snapshots, *, max_polls=20):
        self.snapshots = list(snapshots)
        self.polls = 0
        self.max_polls = max_polls

    async def snapshot(self):
        self.polls += 1
        if self.polls > self.max_polls:
            raise AssertionError("follow() did not settle within the scripted poll budget")
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


def _snap(status, processed):
    return ProgressSnapshot("r1", status, {"discovered": 2, "processed": processed})


def test_follow_stops_at_a_terminal_snapshot_and_prints_plain_lines() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    source = _Scripted([None, _snap("running", 1), _snap("completed", 2)])
    live = LiveProgress(console, source, label="ws", interval=0)

    final = asyncio.run(live.follow())

    assert final is not None and final.status == "completed"
    assert "COMPLETED" in output.getvalue()


def test_follow_renders_live_on_a_terminal() -> None:
    """The Live branch and `live.update` path only run when the console is a terminal."""

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=100)
    source = _Scripted([_snap("running", 1), _snap("completed", 2)])
    live = LiveProgress(console, source, label="ws", interval=0)

    final = asyncio.run(live.follow())

    assert final is not None and final.status == "completed"
    assert "ws" in output.getvalue()


def test_follow_emits_ndjson_events_when_requested(capsys) -> None:
    console = Console(file=StringIO(), force_terminal=False, width=100)
    source = _Scripted([_snap("running", 1), _snap("completed", 2)])
    live = LiveProgress(console, source, label="ws", interval=0, events=True)

    asyncio.run(live.follow())

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["progress", "progress"]
    assert lines[-1]["status"] == "completed"


def test_follow_ends_when_the_awaited_run_finishes() -> None:
    async def scenario():
        console = Console(file=StringIO(), force_terminal=False, width=100)
        run = asyncio.get_running_loop().create_future()
        run.set_result("done")
        live = LiveProgress(console, _Scripted([_snap("running", 1)]), label="ws", interval=0)
        return await live.follow(until=run)

    final = asyncio.run(scenario())
    assert final is not None and final.status == "running"
