from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.sources import ConnectorEvent, DocKey, SyncWindow
from harborrag_core.sources.testing import (
    CONTRACT_CHECKS,
    FAKE_CHILD,
    FAKE_PARENT,
    CheckOutcome,
    ContractCheck,
    ContractScenario,
    FakeCheckpoint,
    FakeItem,
    FakeSourceConnector,
)

START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def _key(kind: str, external_id: str) -> DocKey:
    return DocKey("conn-1", kind, external_id)


def _items() -> list[FakeItem]:
    items = [
        FakeItem(_key(FAKE_PARENT, "at-start"), START, body="first"),
        FakeItem(_key(FAKE_PARENT, "at-end"), END, body="outside"),
        FakeItem(_key(FAKE_PARENT, "old"), START - timedelta(days=3), body="old"),
    ]
    for index in range(7):
        parent = _key(FAKE_PARENT, f"issue-{index}")
        items.append(FakeItem(parent, START + timedelta(hours=index + 1), body=f"issue {index}"))
        for child in range(index % 3):
            items.append(
                FakeItem(
                    _key(FAKE_CHILD, f"comment-{index}-{child}"),
                    START,
                    body=f"comment {child}",
                    parent=parent,
                )
            )
    return items


def _scenario(connector_type: type[FakeSourceConnector] = FakeSourceConnector) -> ContractScenario:
    async def build() -> FakeSourceConnector:
        return connector_type(_items(), page_size=3)

    bad = _key(FAKE_PARENT, "issue-4")

    async def poisoned() -> FakeSourceConnector:
        return connector_type(_items(), page_size=3, failing=[bad])

    return ContractScenario(
        build=build,
        window=SyncWindow(START, END),
        boundary=(_key(FAKE_PARENT, "at-start"), _key(FAKE_PARENT, "at-end")),
        poisoned=(poisoned, bad),
        batch_poisoned=(poisoned, bad),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=str)
async def test_fake_connector_passes_the_contract(check: ContractCheck) -> None:
    assert await check.run(_scenario()) is CheckOutcome.PASSED


class _SkipsAfterResume(FakeSourceConnector):
    """Drops the first item of every resumed read: a classic off-by-one."""

    async def fetch(
        self, window: SyncWindow, checkpoint: FakeCheckpoint | None
    ) -> AsyncIterator[ConnectorEvent[FakeCheckpoint]]:
        if checkpoint is not None and checkpoint.offset:
            checkpoint = checkpoint.model_copy(update={"offset": checkpoint.offset + 1})
        async for event in super().fetch(window, checkpoint):
            yield event


@pytest.mark.asyncio
async def test_resume_check_catches_an_off_by_one_resume() -> None:
    check = next(check for check in CONTRACT_CHECKS if check.name == "resume_equivalence")
    with pytest.raises(AssertionError, match="resuming after checkpoint"):
        await check.run(_scenario(_SkipsAfterResume))


@pytest.mark.asyncio
async def test_checks_without_a_prepared_fixture_skip_instead_of_passing() -> None:
    full = _scenario()
    bare = ContractScenario(build=full.build, window=full.window)
    outcomes = {check.name: await check.run(bare) for check in CONTRACT_CHECKS}
    assert outcomes["window_bounds"] is CheckOutcome.SKIPPED
    assert outcomes["failures_do_not_abort"] is CheckOutcome.SKIPPED
    assert outcomes["bisection_isolates"] is CheckOutcome.SKIPPED
