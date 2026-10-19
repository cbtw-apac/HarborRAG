from __future__ import annotations

import dataclasses
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.sources import (
    ConnectorEvent,
    DocKey,
    SourceAcl,
    SourcePrincipal,
    SourceRecord,
    SyncWindow,
)
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
    collect,
)
from harborrag_core.sources.testing.contract import check_deterministic_bodies

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


class _ShiftingScope(FakeSourceConnector):
    """Reports a different ACL scope on every fetch, across connector instances."""

    _fetches = itertools.count()

    async def fetch(
        self, window: SyncWindow, checkpoint: FakeCheckpoint | None
    ) -> AsyncIterator[ConnectorEvent[FakeCheckpoint]]:
        scope = f"fake:conn-1:read-{next(self._fetches)}"
        async for event in super().fetch(window, checkpoint):
            if isinstance(event, SourceRecord):
                acl = SourceAcl.inherited(realm="fake", scope=scope)
                event = dataclasses.replace(event, acl=acl)
            yield event


class _ShiftingLevels(FakeSourceConnector):
    """Names a different group in a restricted ACL on every fetch."""

    _fetches = itertools.count()

    async def fetch(
        self, window: SyncWindow, checkpoint: FakeCheckpoint | None
    ) -> AsyncIterator[ConnectorEvent[FakeCheckpoint]]:
        group = SourcePrincipal(realm="fake", kind="group", id=f"g-{next(self._fetches)}")
        async for event in super().fetch(window, checkpoint):
            if isinstance(event, SourceRecord):
                acl = SourceAcl.restricted(
                    realm="fake", scope="fake:conn-1", levels=[[group]], reason="r"
                )
                event = dataclasses.replace(event, acl=acl)
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("connector_type", [_ShiftingScope, _ShiftingLevels])
async def test_a_connector_whose_acl_changes_between_reads_fails_the_suite(
    connector_type: type[FakeSourceConnector],
) -> None:
    with pytest.raises(AssertionError, match="rendered different"):
        await check_deterministic_bodies(_scenario(connector_type))


@pytest.mark.asyncio
async def test_fake_records_default_to_an_inherited_acl_in_their_source_scope() -> None:
    connector = FakeSourceConnector(_items(), page_size=3)
    inherited = SourceAcl.inherited(realm="fake", scope="fake:conn-1")
    run = await collect(connector.fetch(SyncWindow.initial(END), None))
    assert run.records
    assert {record.acl for record in run.records} == {inherited}
    keyed = await collect(connector.fetch_keys([_key(FAKE_PARENT, "issue-1")]))
    assert [record.acl for record in keyed.records] == [inherited]


@pytest.mark.asyncio
async def test_a_fake_item_can_carry_its_own_acl() -> None:
    group = SourcePrincipal(realm="fake", kind="group", id="g")
    restricted = SourceAcl.restricted(
        realm="fake", scope="fake:conn-1", levels=[[group]], reason="page_restriction"
    )
    unknown = SourceAcl.unknown(reason="permissions_unreadable")
    connector = FakeSourceConnector(
        [
            FakeItem(_key(FAKE_PARENT, "closed"), START, acl=restricted),
            FakeItem(_key(FAKE_PARENT, "opaque"), START, acl=unknown),
            FakeItem(_key(FAKE_PARENT, "open"), START),
        ]
    )
    run = await collect(connector.fetch(SyncWindow(START, END), None))
    assert {record.key.external_id: record.acl for record in run.records} == {
        "closed": restricted,
        "opaque": unknown,
        "open": SourceAcl.inherited(realm="fake", scope="fake:conn-1"),
    }
