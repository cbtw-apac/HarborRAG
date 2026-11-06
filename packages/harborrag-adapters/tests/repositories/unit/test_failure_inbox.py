"""The failure inbox keeps one item per document: its latest failed result."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import insert

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    TASK_DOCUMENT_RESULTS,
)
from harborrag_adapters.repositories.database.sqlite.client import SQLiteDBClient
from harborrag_core.ingestion import FailurePage, IngestionTask, IngestionTaskState

pytestmark = pytest.mark.unit

BASE = datetime(2026, 11, 1, tzinfo=UTC)
TICKETS = "cpm/tickets"
WIKI = "cpm/wiki"


def _failed(
    code: str, *, stage: str = "ParseAndNormalize", retryable: bool = True
) -> dict[str, object]:
    return {
        "safe_error_code": code,
        "failure_stage": stage,
        "retryable": retryable,
        "source_item_id": f"item-{code}",
    }


class _Seed:
    def __init__(
        self, control_plane: IngestionControlPlaneDatabase, client: SQLiteDBClient
    ) -> None:
        self._control_plane = control_plane
        self._client = client

    async def task(
        self, task_id: str, *, tenant_id: str = "acme", source_key: str = TICKETS
    ) -> None:
        scope = f"scope-{tenant_id}-{source_key}"
        await self._control_plane.source_scans.register_scope(
            tenant_id=tenant_id,
            source_scope_id=scope,
            connector_type="jira",
            connection_id=source_key,
            configuration_fingerprint="f" * 64,
        )
        await self._control_plane.tasks.create(
            IngestionTask(
                task_id=task_id,
                source_scope_id=scope,
                status=IngestionTaskState.PENDING,
                request={"tenant_id": tenant_id},
            )
        )

    async def result(
        self,
        task_id: str,
        document_id: str,
        status: str,
        minutes: int,
        result: dict[str, object] | None = None,
    ) -> None:
        async with self._client.sessions.begin() as session:
            await session.execute(
                insert(TASK_DOCUMENT_RESULTS).values(
                    task_id=task_id,
                    document_id=document_id,
                    document_version_id=None,
                    status=status,
                    result=result or {},
                    completed_at=BASE + timedelta(minutes=minutes),
                )
            )


Inbox = tuple[IngestionControlPlaneDatabase, _Seed]


@pytest_asyncio.fixture
async def inbox(tmp_path: Path) -> AsyncIterator[Inbox]:
    client = SQLiteDBClient(database=str(tmp_path / "control-plane.db"))
    control_plane = IngestionControlPlaneDatabase(client, create_schema=True)
    async with control_plane:
        seed = _Seed(control_plane, client)
        await seed.task("run-1")
        await seed.task("run-2")
        await seed.task("run-other", tenant_id="beta")
        for index in range(12):
            await seed.result(
                "run-1", f"doc-parse-{index:02d}", "failed", index, _failed("parse_failed")
            )
        await seed.result("run-2", "doc-parse-00", "failed", 100, _failed("parse_failed"))
        for index in range(3):
            await seed.result(
                "run-1",
                f"doc-auth-{index}",
                "failed",
                20 + index,
                _failed("auth_expired", stage="FetchAndCaptureRaw"),
            )
        for index in range(2):
            await seed.result(
                "run-1", f"doc-done-{index}", "failed", 30 + index, _failed("parse_failed")
            )
            await seed.result("run-2", f"doc-done-{index}", "published", 200 + index)
        await seed.result("run-1", "doc-fatal", "failed", 40, _failed("bad_input", retryable=False))
        await seed.result("run-other", "doc-foreign", "failed", 50, _failed("parse_failed"))
        yield control_plane, seed


async def _page(control_plane: IngestionControlPlaneDatabase, **overrides: object) -> FailurePage:
    arguments: dict[str, object] = {
        "tenant_id": "acme",
        "source_keys": None,
        "error_category": None,
        "internal_stages": None,
        "resolved": False,
        "q": None,
        "before": None,
        "limit": 100,
        "include_total": False,
    }
    arguments.update(overrides)
    return await control_plane.failures.page(**arguments)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_page_counts_unresolved_failures_per_category(inbox: Inbox) -> None:
    control_plane, _ = inbox

    page = await _page(control_plane, limit=5)

    assert len(page.items) == 5
    assert page.has_more is True
    assert page.counts == {"parse_failed": 12, "auth_expired": 3, "bad_input": 1}
    assert page.total is None


@pytest.mark.asyncio
async def test_page_counts_follow_the_category_filter(inbox: Inbox) -> None:
    control_plane, _ = inbox

    page = await _page(control_plane, error_category="parse_failed")

    assert page.counts == {"parse_failed": 12}
    assert len(page.items) == 12
    assert page.has_more is False


@pytest.mark.asyncio
async def test_total_is_only_returned_on_request(inbox: Inbox) -> None:
    control_plane, _ = inbox

    page = await _page(control_plane, include_total=True)

    assert page.total == 16


@pytest.mark.asyncio
async def test_a_document_failed_twice_is_one_item_with_two_attempts(inbox: Inbox) -> None:
    control_plane, _ = inbox

    page = await _page(control_plane, q="doc-parse-00")

    (item,) = page.items
    assert item.document_id == "doc-parse-00"
    assert item.task_id == "run-2"
    assert item.attempts == 2
    assert item.source_key == TICKETS
    assert item.resolved is False
    assert item.failed_at == BASE + timedelta(minutes=100)


@pytest.mark.asyncio
async def test_resolved_lists_documents_with_a_later_success(inbox: Inbox) -> None:
    control_plane, _ = inbox

    page = await _page(control_plane, resolved=True)

    assert sorted(item.document_id for item in page.items) == ["doc-done-0", "doc-done-1"]
    assert all(item.resolved for item in page.items)
    assert page.counts == {"parse_failed": 2}


@pytest.mark.asyncio
async def test_a_failure_after_a_success_is_unresolved_again(inbox: Inbox) -> None:
    control_plane, seed = inbox
    await seed.task("run-3")
    await seed.result("run-3", "doc-done-0", "failed", 300, _failed("parse_failed"))

    unresolved = await _page(control_plane, q="doc-done-0")
    resolved = await _page(control_plane, q="doc-done-0", resolved=True)

    assert [item.task_id for item in unresolved.items] == ["run-3"]
    assert unresolved.items[0].attempts == 2
    assert resolved.items == ()


@pytest.mark.asyncio
async def test_other_tenants_are_invisible(inbox: Inbox) -> None:
    control_plane, _ = inbox

    page = await _page(control_plane, tenant_id="beta")

    assert [item.document_id for item in page.items] == ["doc-foreign"]
    assert (await _page(control_plane, tenant_id="nobody")).items == ()


@pytest.mark.asyncio
async def test_filters_narrow_by_source_stage_and_text(inbox: Inbox) -> None:
    control_plane, _ = inbox

    wiki = await _page(control_plane, source_keys=frozenset({WIKI}))
    tickets = await _page(control_plane, source_keys=frozenset({TICKETS}), include_total=True)
    capture = await _page(control_plane, internal_stages=frozenset({"FetchAndCaptureRaw"}))
    text = await _page(control_plane, q="AUTH-1")
    by_item = await _page(control_plane, q="ITEM-bad_input")

    assert wiki.items == ()
    assert tickets.total == 16
    assert {item.document_id for item in capture.items} == {
        "doc-auth-0",
        "doc-auth-1",
        "doc-auth-2",
    }
    assert [item.document_id for item in text.items] == ["doc-auth-1"]
    assert [item.document_id for item in by_item.items] == ["doc-fatal"]


@pytest.mark.asyncio
async def test_keyset_pages_walk_every_item_newest_first(inbox: Inbox) -> None:
    control_plane, _ = inbox

    seen: list[str] = []
    before = None
    while True:
        page = await _page(control_plane, limit=5, before=before)
        seen.extend(item.document_id for item in page.items)
        if not page.has_more:
            break
        last = page.items[-1]
        before = (last.failed_at, last.task_id, last.document_id)

    assert len(seen) == 16
    assert len(set(seen)) == 16
    assert seen[0] == "doc-parse-00"


@pytest.mark.asyncio
async def test_select_returns_only_unresolved_retryable_failures(inbox: Inbox) -> None:
    control_plane, _ = inbox

    selected = await control_plane.failures.select(
        tenant_id="acme",
        keys=None,
        source_keys=None,
        error_category=None,
        internal_stages=None,
    )

    ids = {item.document_id for item in selected}
    assert len(selected) == 15
    assert "doc-fatal" not in ids
    assert not ids & {"doc-done-0", "doc-done-1"}


@pytest.mark.asyncio
async def test_select_by_keys_and_limit(inbox: Inbox) -> None:
    control_plane, _ = inbox

    keyed = await control_plane.failures.select(
        tenant_id="acme",
        keys=[("run-2", "doc-parse-00"), ("run-1", "doc-parse-00"), ("run-1", "doc-fatal")],
        source_keys=None,
        error_category=None,
        internal_stages=None,
    )
    limited = await control_plane.failures.select(
        tenant_id="acme",
        keys=None,
        source_keys=None,
        error_category="parse_failed",
        internal_stages=None,
        limit=4,
    )

    assert [(item.task_id, item.document_id) for item in keyed] == [("run-2", "doc-parse-00")]
    assert len(limited) == 4


_KNOWN_STAGES = frozenset({"FetchAndCaptureRaw", "ParseAndNormalize"})


@pytest.mark.asyncio
async def test_unmapped_stages_match_the_stage_filter_when_the_known_set_is_given(
    inbox: Inbox,
) -> None:
    control_plane, seed = inbox
    await seed.result(
        "run-1", "doc-no-stage", "failed", 60, {"safe_error_code": "odd", "retryable": True}
    )
    await seed.result("run-1", "doc-new-stage", "failed", 61, _failed("odd", stage="Brand New"))

    strict = await _page(control_plane, internal_stages=frozenset({"FetchAndCaptureRaw"}))
    capture = await _page(
        control_plane,
        internal_stages=frozenset({"FetchAndCaptureRaw"}),
        known_stages=_KNOWN_STAGES,
    )
    interpret = await _page(control_plane, internal_stages=frozenset({"ParseAndNormalize"}))
    selected = await control_plane.failures.select(
        tenant_id="acme",
        keys=None,
        source_keys=None,
        error_category="odd",
        internal_stages=frozenset({"FetchAndCaptureRaw"}),
        known_stages=_KNOWN_STAGES,
    )

    assert {item.document_id for item in strict.items} == {"doc-auth-0", "doc-auth-1", "doc-auth-2"}
    assert {item.document_id for item in capture.items} == {
        "doc-auth-0",
        "doc-auth-1",
        "doc-auth-2",
        "doc-no-stage",
        "doc-new-stage",
    }
    assert "doc-no-stage" not in {item.document_id for item in interpret.items}
    assert {item.document_id for item in selected} == {"doc-no-stage", "doc-new-stage"}


@pytest.mark.asyncio
async def test_the_unknown_category_matches_failures_without_an_error_code(inbox: Inbox) -> None:
    control_plane, seed = inbox
    await seed.result("run-1", "doc-bare", "failed", 70, {"retryable": True})

    page = await _page(control_plane, error_category="unknown")
    selected = await control_plane.failures.select(
        tenant_id="acme",
        keys=None,
        source_keys=None,
        error_category="unknown",
        internal_stages=None,
    )

    assert [item.document_id for item in page.items] == ["doc-bare"]
    assert page.counts == {"unknown": 1}
    assert [item.document_id for item in selected] == ["doc-bare"]
