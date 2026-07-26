from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from harborrag_adapters.connectors.exceptions import AuthenticationError
from harborrag_core.domain.source import SourceRecord
from harborrag_runtime.temporal.activities.discovery import DiscoveryActivities
from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
    DiscoveryInput,
    DiscoveryResult,
)

from .fakes import Connector, Dependencies, DiscoveryState


@pytest.mark.asyncio
async def test_discovery_returns_references_and_heartbeats(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    records = (
        SourceRecord(id="a", source_type="text/plain", locator="a"),
        SourceRecord(id="b", source_type="text/plain", locator="b"),
    )
    state = DiscoveryState()
    activities = DiscoveryActivities(Dependencies(Connector(records), state))
    environment = ActivityEnvironment()
    heartbeats = []
    environment.on_heartbeat = heartbeats.append

    result = await environment.run(
        activities.discover_artifacts,
        DiscoveryInput(
            run_id="run-1",
            tenant_id="tenant-1",
            manifest_id="manifest-1",
            connector_name="local",
            cursor=None,
            page_size=10,
        ),
    )

    assert tuple(item.artifact_id for item in result.artifacts) == ("a", "b")
    assert result.done is True
    assert result.checkpoint_ref == "checkpoint://discovery/2"
    assert heartbeats[-1].checkpoint_ref == result.checkpoint_ref
    state.initialize_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_uses_connector_provider_for_source_kind(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    record = SourceRecord(
        id="jira://ENG/ENG-1",
        source_type="application/vnd.atlassian.jira.issue+json",
        locator="ENG-1",
    )
    state = DiscoveryState()
    activities = DiscoveryActivities(
        Dependencies(Connector((record,), provider_name="jira"), state)
    )

    result = await ActivityEnvironment().run(
        activities.discover_artifacts,
        DiscoveryInput(
            run_id="run-1",
            tenant_id="tenant-1",
            manifest_id="manifest-1",
            connector_name="team-jira",
            cursor=None,
            page_size=10,
        ),
    )

    assert result.artifacts[0].source_kind == "jira"
    assert record.metadata["source_kind"] == "jira"


@pytest.mark.asyncio
async def test_discovery_preserves_non_retryable_adapter_error(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    activities = DiscoveryActivities(
        Dependencies(Connector(error=AuthenticationError("invalid credential")), DiscoveryState())
    )

    with pytest.raises(AuthenticationError):
        await ActivityEnvironment().run(
            activities.discover_artifacts,
            DiscoveryInput(
                run_id="run-1",
                tenant_id="tenant-1",
                manifest_id="manifest-1",
                connector_name="local",
                cursor=None,
                page_size=10,
            ),
        )


@pytest.mark.asyncio
async def test_discovery_resume_keeps_references_persisted_before_retry(
    monkeypatch,
) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    records = (
        SourceRecord(id="a", source_type="text/plain", locator="a"),
        SourceRecord(id="b", source_type="text/plain", locator="b"),
    )
    request = DiscoveryInput(
        run_id="run-1",
        tenant_id="tenant-1",
        manifest_id="manifest-1",
        connector_name="local",
        cursor=None,
        page_size=10,
    )
    state = DiscoveryState()
    state.discovery_progress.return_value = DiscoveryResult(
        artifacts=(ArtifactReference("a", "source://a", "local", "local"),),
        next_cursor="1",
        checkpoint_ref="checkpoint://partial",
        done=False,
    )

    result = await ActivityEnvironment().run(
        DiscoveryActivities(Dependencies(Connector(records), state)).discover_artifacts,
        request,
    )

    assert tuple(item.artifact_id for item in result.artifacts) == ("a", "b")
    assert result.done is True
