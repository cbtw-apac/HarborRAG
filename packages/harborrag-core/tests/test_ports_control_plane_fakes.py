"""Contract tests for the reusable in-memory core port implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job
from harborrag_core.domain.member import Member
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source import SourceRecord
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.testing.control_plane_fakes import (
    FakeActivityRepository,
    FakeJobRepository,
    FakeMemberRepository,
    FakePendingEffectRepository,
    FakeProjectRepository,
    FakeProviderRepository,
    FakeSettingsRepository,
    FakeSourceRepository,
)
from harborrag_core.testing.fakes import (
    FakeConnector,
    FakeEventBus,
    FakeJobQueue,
    FakeParser,
    FakeSecrets,
)

pytestmark = pytest.mark.whitebox


def _project(project_id: str = "p1") -> Project:
    return Project(
        id=project_id,
        tenant_id="DEFAULT",
        name=f"Project {project_id}",
        collection=f"collection_{project_id}",
    )


def _source(source_id: str = "s1", project_id: str = "p1") -> SourceConfig:
    return SourceConfig(
        id=source_id,
        tenant_id="DEFAULT",
        project_id=project_id,
        source_type="local_file",
        name=f"Source {source_id}",
    )


def _job(job_id: str, *, source_id: str = "s1", status: str = "queued") -> Job:
    return Job(
        id=job_id,
        tenant_id="DEFAULT",
        source_id=source_id,
        project_id="p1",
        job_type="bulk_ingest",
        status=status,  # type: ignore[arg-type]
    )


def _status(job: Job) -> str:
    """Read mutable status without narrowing it across repository calls."""
    return job.status


def _activity(entry_id: str, created_at: datetime) -> ActivityEntry:
    return ActivityEntry(
        id=entry_id,
        tenant_id="DEFAULT",
        actor="tester",
        verb="created",
        entity_type="source",
        entity_id="s1",
        summary=f"created {entry_id}",
        created_at=created_at,
    )


def test_fake_connector_and_parser_obey_the_discovery_load_parse_contract() -> None:
    raw = RawDocument(
        id="doc-1",
        source="file:///doc-1.txt",
        content=b"hello",
        content_type="text/plain",
    )
    connector = FakeConnector(documents=[raw])

    assert list(connector.discover()) == [
        SourceRecord(id="doc-1", source_type="text/plain", locator="file:///doc-1.txt")
    ]
    record = next(iter(connector.discover()))
    assert connector.load(record) is raw
    with pytest.raises(KeyError, match="missing"):
        connector.load(SourceRecord(id="missing", source_type="text/plain", locator="missing"))

    parsed = FakeParser(parser_name="fixture").parse(raw)
    assert parsed.content == "hello"
    assert parsed.parser_name == "fixture"
    elements = parsed.elements
    assert elements is not None
    assert [(element.id, element.type, element.content) for element in elements] == [
        ("doc-1:0", "paragraph", "hello")
    ]


@pytest.mark.asyncio
async def test_fake_project_and_source_repositories_cover_crud_and_filters() -> None:
    projects = FakeProjectRepository()
    first = _project()
    assert await projects.get(first.id, tenant_ids=None) is None
    assert await projects.create(first) is first
    assert await projects.list(tenant_ids=None) == [first]
    first.name = "Updated"
    assert await projects.update(first) is first
    got = await projects.get(first.id, tenant_ids=None)
    assert got is not None and got.name == "Updated"
    await projects.delete(first.id, tenant_ids=None)
    assert await projects.list(tenant_ids=None) == []

    sources = FakeSourceRepository()
    first_source = _source()
    second_source = _source("s2", "p2")
    assert await sources.get("missing", tenant_ids=None) is None
    assert await sources.create(first_source) is first_source
    assert await sources.create(second_source) is second_source
    assert await sources.list(tenant_ids=None) == [first_source, second_source]
    assert await sources.list("p1", tenant_ids=None) == [first_source]
    first_source.name = "Updated"
    assert await sources.update(first_source) is first_source
    await sources.delete(first_source.id, tenant_ids=None)
    assert await sources.get(first_source.id, tenant_ids=None) is None


@pytest.mark.asyncio
async def test_fake_project_and_source_repositories_enforce_tenant_scope() -> None:
    """A tenant_ids scope that excludes a record must hide it from list/get,
    and must not delete it -- the exact defect class this port closed."""
    projects = FakeProjectRepository()
    mine = _project("mine")
    theirs = Project(id="theirs", tenant_id="tenant-b", name="Theirs", collection="theirs")
    await projects.create(mine)
    await projects.create(theirs)
    scope = frozenset({"DEFAULT"})

    assert [p.id for p in await projects.list(tenant_ids=scope)] == ["mine"]
    assert await projects.get("theirs", tenant_ids=scope) is None
    assert await projects.get("mine", tenant_ids=scope) is mine

    await projects.delete("theirs", tenant_ids=scope)
    assert await projects.get("theirs", tenant_ids=None) is theirs  # untouched
    await projects.delete("mine", tenant_ids=scope)
    assert await projects.get("mine", tenant_ids=None) is None  # in-scope delete works

    sources = FakeSourceRepository()
    mine_source = _source("s-mine")
    their_source = SourceConfig(
        id="s-theirs",
        tenant_id="tenant-b",
        project_id="p1",
        source_type="local_file",
        name="Theirs",
    )
    await sources.create(mine_source)
    await sources.create(their_source)

    assert [s.id for s in await sources.list(tenant_ids=scope)] == ["s-mine"]
    assert await sources.get("s-theirs", tenant_ids=scope) is None
    await sources.delete("s-theirs", tenant_ids=scope)
    assert await sources.get("s-theirs", tenant_ids=None) is their_source  # untouched


@pytest.mark.asyncio
async def test_fake_job_repository_filters_counts_and_records_events() -> None:
    repository = FakeJobRepository()
    queued = _job("queued")
    running = _job("running", source_id="s2", status="running")
    assert await repository.get("missing", tenant_ids=None) is None
    assert await repository.save(queued) is queued
    assert await repository.save(running) is running
    assert await repository.list(tenant_ids=None) == [queued, running]
    assert await repository.list(status="running", tenant_ids=None) == [running]
    assert await repository.list(source_id="s1", tenant_ids=None) == [queued]
    assert await repository.list(status="running", source_id="s2", tenant_ids=None) == [running]
    assert await repository.count_by_status(tenant_ids=None) == {"queued": 1, "running": 1}

    event = HarborEvent(name="job.started", trace_id="trace-1")
    await repository.append_event(running.id, event)
    assert repository.events == {running.id: [event]}


@pytest.mark.asyncio
async def test_fake_job_repository_enforces_tenant_scope() -> None:
    repository = FakeJobRepository()
    mine = _job("mine")
    theirs = Job(
        id="theirs",
        tenant_id="tenant-b",
        source_id="s1",
        project_id="p1",
        job_type="bulk_ingest",
    )
    await repository.save(mine)
    await repository.save(theirs)
    scope = frozenset({"DEFAULT"})

    assert [j.id for j in await repository.list(tenant_ids=scope)] == ["mine"]
    assert await repository.get("theirs", tenant_ids=scope) is None
    assert await repository.count_by_status(tenant_ids=scope) == {"queued": 1}


@pytest.mark.asyncio
async def test_fake_activity_settings_provider_and_member_repositories() -> None:
    now = datetime.now(UTC)
    activity = FakeActivityRepository()
    older = _activity("older", now - timedelta(minutes=1))
    newer = _activity("newer", now)
    await activity.append(older)
    await activity.append(newer)
    assert await activity.list(limit=1, tenant_ids=None) == [newer]

    settings = FakeSettingsRepository()
    assert await settings.get() == WorkspaceSettings(tenant_id="DEFAULT")
    replacement = WorkspaceSettings(tenant_id="DEFAULT", data={"theme": "dark"})
    assert await settings.put(replacement) is replacement
    assert await settings.get() is replacement

    providers = FakeProviderRepository()
    provider = Provider(id="provider-1", tenant_id="DEFAULT", name="Local", family="chat")
    assert await providers.get(provider.id, tenant_ids=None) is None
    assert await providers.save(provider) is provider
    assert await providers.list(tenant_ids=None) == [provider]
    await providers.delete(provider.id, tenant_ids=None)
    assert await providers.list(tenant_ids=None) == []

    members = FakeMemberRepository()
    member = Member(id="member-1", tenant_id="DEFAULT", subject="user-1")
    assert await members.get_by_subject(member.subject) is None
    assert await members.save(member) is member
    assert await members.list(tenant_ids=None) == [member]
    assert await members.get_by_subject(member.subject) is member
    assert await members.get_by_subject("unknown") is None
    await members.delete(member.id, tenant_ids=None)
    assert await members.list(tenant_ids=None) == []


@pytest.mark.asyncio
async def test_fake_activity_provider_and_member_repositories_enforce_tenant_scope() -> None:
    now = datetime.now(UTC)
    scope = frozenset({"DEFAULT"})

    activity = FakeActivityRepository()
    mine = _activity("mine", now)
    theirs = ActivityEntry(
        id="theirs",
        tenant_id="tenant-b",
        actor="tester",
        verb="created",
        entity_type="source",
        entity_id="s1",
        summary="created theirs",
        created_at=now,
    )
    await activity.append(mine)
    await activity.append(theirs)
    assert [e.id for e in await activity.list(limit=50, tenant_ids=scope)] == ["mine"]

    providers = FakeProviderRepository()
    mine_provider = Provider(id="mine", tenant_id="DEFAULT", name="Local", family="chat")
    their_provider = Provider(id="theirs", tenant_id="tenant-b", name="Other", family="chat")
    await providers.save(mine_provider)
    await providers.save(their_provider)
    assert [p.id for p in await providers.list(tenant_ids=scope)] == ["mine"]
    assert await providers.get("theirs", tenant_ids=scope) is None
    await providers.delete("theirs", tenant_ids=scope)
    assert await providers.get("theirs", tenant_ids=None) is their_provider  # untouched

    members = FakeMemberRepository()
    mine_member = Member(id="mine", tenant_id="DEFAULT", subject="user-1")
    their_member = Member(id="theirs", tenant_id="tenant-b", subject="user-2")
    await members.save(mine_member)
    await members.save(their_member)
    assert [m.id for m in await members.list(tenant_ids=scope)] == ["mine"]
    await members.delete("theirs", tenant_ids=scope)
    assert await members.list(tenant_ids=None) == [mine_member, their_member]  # untouched


@pytest.mark.asyncio
async def test_fake_pending_effect_repository_is_oldest_first_and_completion_removes_it() -> None:
    repository = FakePendingEffectRepository()
    first = PendingControlPlaneEffect(id="eff-1", kind="retire_secret", payload={"ref": "s1"})
    second = PendingControlPlaneEffect(id="eff-2", kind="log_activity", payload={"id": "act-1"})
    await repository.enqueue(first)
    await repository.enqueue(second)

    assert [effect.id for effect in await repository.list_pending()] == ["eff-1", "eff-2"]
    assert [effect.id for effect in await repository.list_pending(limit=1)] == ["eff-1"]

    await repository.complete("eff-1")
    assert [effect.id for effect in await repository.list_pending()] == ["eff-2"]
    await repository.complete("eff-1")  # already gone: a no-op, not an error
    assert [effect.id for effect in await repository.list_pending()] == ["eff-2"]


@pytest.mark.asyncio
async def test_fake_job_queue_handles_skips_retries_failures_and_cancellation() -> None:
    queue = FakeJobQueue()
    already_done = _job("already-done", status="succeeded")
    retrying = _job("retrying")
    await queue.enqueue(already_done)
    assert await queue.enqueue(retrying) is retrying

    assert await queue.claim_next(lease_seconds=30) is retrying
    assert _status(retrying) == "running" and retrying.attempts == 1
    await queue.mark_failed(retrying.id, "temporary", retryable=True)
    assert _status(retrying) == "queued" and retrying.last_error == "temporary"
    assert await queue.claim_next(lease_seconds=30) is retrying
    await queue.mark_failed(retrying.id, "permanent", retryable=False)
    assert _status(retrying) == "failed" and retrying.last_error == "permanent"

    successful = _job("successful")
    await queue.enqueue(successful)
    assert await queue.claim_next(lease_seconds=30) is successful
    await queue.mark_done(successful.id)
    assert _status(successful) == "succeeded"

    cancelled = _job("cancelled")
    await queue.enqueue(cancelled)
    await queue.cancel(cancelled.id)
    assert _status(cancelled) == "cancelled"
    assert await queue.claim_next(lease_seconds=30) is None


@pytest.mark.asyncio
async def test_fake_secrets_and_event_bus_are_opaque_and_deterministic() -> None:
    secrets = FakeSecrets()
    first_ref = await secrets.put("alpha")
    second_ref = await secrets.put("beta")
    assert (first_ref, second_ref) == ("secret://fake/1", "secret://fake/2")
    assert await secrets.resolve(first_ref) == "alpha"
    await secrets.delete(first_ref)
    await secrets.delete("secret://fake/missing")
    with pytest.raises(KeyError):
        await secrets.resolve(first_ref)

    bus = FakeEventBus()
    started = HarborEvent(name="job.started", trace_id="trace-1")
    finished = HarborEvent(name="job.finished", trace_id="trace-2")
    ignored = HarborEvent(name="source.created", trace_id="trace-3")
    for event in (started, finished, ignored):
        await bus.publish(event)
    assert [event async for event in bus.subscribe("job.")] == [started, finished]
    assert [event async for event in bus.subscribe("missing.")] == []
