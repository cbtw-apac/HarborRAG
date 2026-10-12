"""Behaviour every source connector must show, independent of its vendor.

The checks drive a connector only through the port, so the same suite runs
against the in-memory fake, synthetic HTTP fixtures, and recorded traffic.
They raise ``AssertionError`` and never import pytest; a test module
parametrizes over :data:`CONTRACT_CHECKS`.

Preflight (the spec's case 11) depends on each vendor's probes and is tested
per connector.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from ..events import (
    CheckpointMark,
    ConnectorEvent,
    DocKey,
    ItemFailure,
    SourceRecord,
    SyncWindow,
    Tombstone,
)
from ..port import SourceConnector

type ConnectorFactory = Callable[[], Awaitable[SourceConnector[Any]]]


@dataclass(frozen=True)
class ContractScenario:
    """One connector against one fixture state.

    ``build`` returns a fresh connector over the fixture. Optional fields
    enable the checks that need a specially prepared fixture; a check whose
    fixture is missing reports ``SKIPPED`` rather than passing silently.
    """

    build: ConnectorFactory
    window: SyncWindow
    boundary: tuple[DocKey, DocKey] | None = None
    """Keys updated exactly at ``window.start`` and exactly at ``window.end``."""
    poisoned: tuple[ConnectorFactory, DocKey] | None = None
    """A connector whose read of one item fails, and that item's key."""
    batch_poisoned: tuple[ConnectorFactory, DocKey] | None = None
    """A connector with one bad item inside a batched read."""
    listed_kinds: frozenset[str] | None = None
    """Kinds ``list_keys`` must cover; ``None`` means every kind."""
    fetchable_kinds: frozenset[str] | None = None
    """Kinds ``fetch_keys`` accepts directly; ``None`` means every kind."""


class CheckOutcome(StrEnum):
    PASSED = "passed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ContractCheck:
    name: str
    run: Callable[[ContractScenario], Awaitable[CheckOutcome]]

    def __str__(self) -> str:
        return self.name


@dataclass
class Run:
    """Everything one ``fetch`` yielded, split by event type."""

    events: list[ConnectorEvent[Any]] = field(default_factory=list)

    @property
    def records(self) -> list[SourceRecord]:
        return [event for event in self.events if isinstance(event, SourceRecord)]

    @property
    def tombstones(self) -> list[Tombstone]:
        return [event for event in self.events if isinstance(event, Tombstone)]

    @property
    def failures(self) -> list[ItemFailure]:
        return [event for event in self.events if isinstance(event, ItemFailure)]

    @property
    def marks(self) -> list[tuple[int, CheckpointMark[Any]]]:
        return [
            (index, event)
            for index, event in enumerate(self.events)
            if isinstance(event, CheckpointMark)
        ]

    def versions(self) -> set[tuple[DocKey, str]]:
        return {(record.key, record.version) for record in self.records}

    def keys(self) -> set[DocKey]:
        return {record.key for record in self.records}


async def collect(stream: AsyncIterator[ConnectorEvent[Any]]) -> Run:
    """Drain one event stream."""

    run = Run()
    async for event in stream:
        run.events.append(event)
    return run


async def _fetch(
    scenario: ContractScenario,
    window: SyncWindow | None = None,
    checkpoint: object = None,
    *,
    build: ConnectorFactory | None = None,
) -> Run:
    connector = await (build or scenario.build)()
    parsed = None if checkpoint is None else connector.parse_checkpoint(checkpoint)
    return await collect(connector.fetch(window or scenario.window, parsed))


def _heartbeat(checkpoint: BaseModel) -> object:
    """The checkpoint as the runtime stores it: JSON-shaped heartbeat details."""

    return checkpoint.model_dump(mode="json")


def _outcome_set(run: Run) -> set[tuple[str, DocKey, str]]:
    found: set[tuple[str, DocKey, str]] = {
        ("record", key, version) for key, version in run.versions()
    }
    found |= {("tombstone", tombstone.key, "") for tombstone in run.tombstones}
    found |= {("failure", failure.key, failure.reason) for failure in run.failures if failure.key}
    return found


def _merged_state(events: list[ConnectorEvent[Any]]) -> dict[str, str]:
    state: dict[str, str] = {}
    for event in events:
        if isinstance(event, CheckpointMark):
            state.update(event.state)
    return state


async def check_idempotent(scenario: ContractScenario) -> CheckOutcome:
    first, second = await _fetch(scenario), await _fetch(scenario)
    assert first.records, "the scenario window must contain at least one record"
    assert _outcome_set(first) == _outcome_set(second), "the same window read twice differs"
    return CheckOutcome.PASSED


async def check_resume_equivalence(scenario: ContractScenario) -> CheckOutcome:
    full = await _fetch(scenario)
    marks = full.marks
    assert marks, "fetch must yield at least one CheckpointMark"
    expected = _outcome_set(full)
    expected_state = _merged_state(full.events)
    for index, mark in marks:
        prefix = Run(full.events[: index + 1])
        resumed = await _fetch(scenario, checkpoint=_heartbeat(mark.checkpoint))
        combined = Run(prefix.events + resumed.events)
        assert _outcome_set(combined) == expected, (
            f"resuming after checkpoint #{marks.index((index, mark)) + 1} "
            "does not yield the uninterrupted result"
        )
        assert _merged_state(combined.events) == expected_state, (
            "resumed run proposes different cursor state"
        )
    return CheckOutcome.PASSED


async def check_window_bounds(scenario: ContractScenario) -> CheckOutcome:
    if scenario.boundary is None:
        return CheckOutcome.SKIPPED
    at_start, at_end = scenario.boundary
    keys = (await _fetch(scenario)).keys()
    assert at_start in keys, "an item updated exactly at window.start must be included"
    assert at_end not in keys, "an item updated exactly at window.end must be excluded"
    return CheckOutcome.PASSED


async def _check_isolated(
    scenario: ContractScenario, poisoned: tuple[ConnectorFactory, DocKey] | None
) -> CheckOutcome:
    if poisoned is None:
        return CheckOutcome.SKIPPED
    build, bad = poisoned
    clean = await _fetch(scenario)
    run = await _fetch(scenario, build=build)
    failed = [failure.key for failure in run.failures]
    assert failed == [bad], f"expected exactly one failure for {bad}, got {failed}"
    missing = clean.keys() - _with_descendants(clean, bad) - run.keys()
    assert not missing, f"one bad item also lost {sorted(map(str, missing))}"
    return CheckOutcome.PASSED


def _with_descendants(run: Run, root: DocKey) -> set[DocKey]:
    """``root`` plus every record below it: those may go missing with their parent."""

    found = {root}
    grew = True
    while grew:
        below = {record.key for record in run.records if record.parent in found}
        grew = not below <= found
        found |= below
    return found


async def check_failures_do_not_abort(scenario: ContractScenario) -> CheckOutcome:
    return await _check_isolated(scenario, scenario.poisoned)


async def check_bisection_isolates(scenario: ContractScenario) -> CheckOutcome:
    return await _check_isolated(scenario, scenario.batch_poisoned)


def _initial(scenario: ContractScenario) -> SyncWindow:
    return SyncWindow.initial(scenario.window.end)


def _of_kinds(keys: set[DocKey], kinds: frozenset[str] | None) -> set[DocKey]:
    return keys if kinds is None else {key for key in keys if key.kind in kinds}


async def check_listing_covers_fetch(scenario: ContractScenario) -> CheckOutcome:
    fetched = _of_kinds((await _fetch(scenario, _initial(scenario))).keys(), scenario.listed_kinds)
    connector = await scenario.build()
    listed = {key async for key in connector.list_keys()}
    missing = fetched - listed
    assert not missing, f"list_keys misses fetched keys {sorted(map(str, missing))[:5]}"
    return CheckOutcome.PASSED


async def check_targeted_fetch(scenario: ContractScenario) -> CheckOutcome:
    known = sorted(_of_kinds((await _fetch(scenario)).keys(), scenario.fetchable_kinds))[:50]
    assert known, "the scenario window must contain a fetchable record"
    unknown = known[0].sibling(known[0].kind, "contract-missing-item")
    requested = [*known, unknown]
    run = await collect((await scenario.build()).fetch_keys(requested))
    answered = run.keys() | {t.key for t in run.tombstones} | {f.key for f in run.failures}
    unanswered = set(requested) - answered
    assert not unanswered, f"fetch_keys left {sorted(map(str, unanswered))} unanswered"
    assert unknown not in run.keys(), "an unknown key must not come back as a record"
    return CheckOutcome.PASSED


async def check_children_complete(scenario: ContractScenario) -> CheckOutcome:
    run = await _fetch(scenario, _initial(scenario))
    parents = [record for record in run.records if record.children is not None]
    if not parents:
        return CheckOutcome.SKIPPED
    failed = {failure.key for failure in run.failures}
    for parent in parents:
        claimed = parent.children or frozenset()
        kinds = {child.kind for child in claimed}
        emitted = {
            record.key
            for record in run.records
            if record.parent == parent.key and record.key.kind in kinds
        }
        assert emitted <= claimed, f"{parent.key} emits children it does not list"
        assert claimed <= emitted | failed, f"{parent.key} lists children it never emits"
    return CheckOutcome.PASSED


def _rendered(run: Run) -> Mapping[DocKey, tuple[str, str, object]]:
    return {r.key: (r.title, r.body, r.metadata) for r in run.records}


async def check_deterministic_bodies(scenario: ContractScenario) -> CheckOutcome:
    first, second = await _fetch(scenario), await _fetch(scenario)
    assert _rendered(first) == _rendered(second), "two reads rendered different bodies"
    return CheckOutcome.PASSED


async def check_foreign_checkpoint(scenario: ContractScenario) -> CheckOutcome:
    window = scenario.window
    foreign_window = SyncWindow(window.start, window.end + timedelta(seconds=1), window.state)
    foreign_marks = (await _fetch(scenario, foreign_window)).marks
    connector = await scenario.build()
    assert connector.parse_checkpoint("not a checkpoint") is None
    assert connector.parse_checkpoint({"version": 10_000}) is None
    if not foreign_marks:
        return CheckOutcome.PASSED
    expected = _outcome_set(await _fetch(scenario))
    foreign = _heartbeat(foreign_marks[0][1].checkpoint)
    resumed = await _fetch(scenario, checkpoint=foreign)
    assert _outcome_set(resumed) == expected, "a checkpoint from another window was reused"
    return CheckOutcome.PASSED


CONTRACT_CHECKS: tuple[ContractCheck, ...] = (
    ContractCheck("idempotent", check_idempotent),
    ContractCheck("resume_equivalence", check_resume_equivalence),
    ContractCheck("window_bounds", check_window_bounds),
    ContractCheck("failures_do_not_abort", check_failures_do_not_abort),
    ContractCheck("bisection_isolates", check_bisection_isolates),
    ContractCheck("listing_covers_fetch", check_listing_covers_fetch),
    ContractCheck("targeted_fetch", check_targeted_fetch),
    ContractCheck("children_complete", check_children_complete),
    ContractCheck("deterministic_bodies", check_deterministic_bodies),
    ContractCheck("foreign_checkpoint", check_foreign_checkpoint),
)
