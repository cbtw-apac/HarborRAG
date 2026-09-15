"""Inline Rich rendering of ingestion progress (no full-screen UI)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from harborrag_app.cli.progress import ProgressSnapshot, ProgressSource
from harborrag_app.cli.rendering_values import STATUS_STYLES, integer
from harborrag_app.cli.stages import stage_line

_BAR_WIDTH = 40
_COUNTERS = (
    ("succeeded", "green"),
    ("unchanged", "dim"),
    ("skipped", "yellow"),
    ("failed", "red"),
)
_MAX_ATTENTION = 5
_PLAIN_LINE_SECONDS = 5.0


def render_snapshot(
    snapshot: ProgressSnapshot | None,
    *,
    label: str,
    elapsed: float,
) -> RenderableType:
    """Compact block: header, stage strip, bar, counters, and the last failed artifacts."""

    header = Text.assemble(("⚓ ", "cyan"), (label, "bold"))
    if snapshot is None:
        header.append("  waiting for the run to register…", style="dim")
        header.append(f"  {_clock(elapsed)}", style="dim")
        return header
    header.append(" · ")
    header.append(snapshot.run_id, style="cyan")
    header.append(" · ")
    header.append(
        snapshot.status.upper(),
        style=f"bold {STATUS_STYLES.get(snapshot.status, 'white')}",
    )
    header.append(f" · {_clock(elapsed)}", style="dim")
    rows: list[RenderableType] = [
        header,
        stage_line(snapshot.status, snapshot.progress),
        _bar(snapshot),
        _counters(snapshot),
    ]
    if snapshot.message:
        rows.append(Text(snapshot.message, style="dim"))
    rows.extend(
        Text(f"✗ {artifact}", style="red")
        for artifact in snapshot.failed_artifacts[-_MAX_ATTENTION:]
    )
    return Group(*rows)


def _bar(snapshot: ProgressSnapshot) -> RenderableType:
    discovered = integer(snapshot.progress.get("discovered"))
    processed = integer(snapshot.progress.get("processed"))
    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    if discovered:
        grid.add_row(
            ProgressBar(total=discovered, completed=min(processed, discovered), width=_BAR_WIDTH),
            Text(f"{processed:,}/{discovered:,} documents", style="bold"),
        )
    else:
        grid.add_row(
            ProgressBar(total=None, pulse=True, width=_BAR_WIDTH),
            Text("discovering…", style="dim"),
        )
    return grid


def _counters(snapshot: ProgressSnapshot) -> Text:
    line = Text()
    for name, style in _COUNTERS:
        if line:
            line.append(" · ", style="dim")
        line.append(f"{name} {integer(snapshot.progress.get(name)):,}", style=style)
    return line


def _clock(elapsed: float) -> str:
    minutes, seconds = divmod(int(elapsed), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class LiveProgress:
    """Poll a source and render until the run is terminal or ``until`` completes.

    TTY: a ``rich.live.Live`` block redrawn in place. Non-TTY: one plain status block per
    change, at most every five seconds. ``events=True``: NDJSON snapshots on stdout.
    """

    def __init__(  # noqa: PLR0913 - one parameter per presentation choice
        self,
        console: Console,
        source: ProgressSource,
        *,
        label: str,
        interval: float = 1.0,
        events: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console
        self._source = source
        self._label = label
        self._interval = interval
        self._events = events
        self._clock = clock
        self._last_key: object = object()
        self._last_plain_at = float("-inf")

    async def follow(self, until: asyncio.Future[Any] | None = None) -> ProgressSnapshot | None:
        started = self._clock()
        live_mode = self._console.is_terminal and not self._events
        context: AbstractContextManager[Any] = (
            Live(
                render_snapshot(None, label=self._label, elapsed=0.0),
                console=self._console,
                refresh_per_second=4,
                transient=False,
            )
            if live_mode
            else nullcontext()
        )
        snapshot: ProgressSnapshot | None = None
        with context as live:
            while True:
                snapshot = await self._source.snapshot()
                self._emit(snapshot, live, elapsed=self._clock() - started)
                if snapshot is not None and snapshot.terminal:
                    return snapshot
                if until is not None and until.done():
                    # One last read so the final counters land before the summary.
                    final = await self._source.snapshot()
                    self._emit(final, live, elapsed=self._clock() - started, force=True)
                    return final or snapshot
                await asyncio.sleep(self._interval)

    def _emit(
        self,
        snapshot: ProgressSnapshot | None,
        live: Live | None,
        *,
        elapsed: float,
        force: bool = False,
    ) -> None:
        key: object = None
        if snapshot is not None:
            key = (
                snapshot.status,
                tuple(sorted(snapshot.progress.items())),
                snapshot.failed_artifacts,
            )
        changed = key != self._last_key
        self._last_key = key
        if live is not None:
            live.update(render_snapshot(snapshot, label=self._label, elapsed=elapsed))
            return
        if snapshot is None:
            return
        if self._events:
            if changed or force:
                payload = {"event": "progress", **snapshot.as_dict()}
                print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            return
        due = elapsed - self._last_plain_at >= _PLAIN_LINE_SECONDS
        if force or snapshot.terminal or (changed and due):
            self._last_plain_at = elapsed
            self._console.print(render_snapshot(snapshot, label=self._label, elapsed=elapsed))


__all__ = ["LiveProgress", "render_snapshot"]
