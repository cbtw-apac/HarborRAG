from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactReference,
    ArtifactStage,
    ArtifactStageState,
)


class Observer:
    def record(self, event, attributes) -> None:
        return None


class Connector:
    def __init__(
        self,
        records=(),
        error: Exception | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.records = tuple(records)
        self.error = error
        self.provider_name = provider_name

    def discover(self, query):
        if self.error is not None:
            raise self.error
        return iter(self.records)


class DiscoveryState:
    def __init__(self) -> None:
        self.initialize_run = AsyncMock()
        self.discovery_progress = AsyncMock(return_value=None)
        self.save_discovery_progress = AsyncMock(return_value="checkpoint://discovery/2")

    async def persist_discovered(self, request, source):
        return ArtifactReference(
            artifact_id=source.id,
            source_ref=f"source://{source.id}",
            source_kind=source.metadata.get("source_kind", "local"),
            connector_name=request.connector_name,
            checksum=source.checksum,
        )


class Dependencies:
    def __init__(self, connector, state) -> None:
        self._connector = connector
        self.state = state
        self.observer = Observer()

    def connector(self, name):
        return self._connector


def fetch_request() -> ArtifactActivityInput:
    artifact = ArtifactReference("a", "source://a", "jira", "jira")
    return ArtifactActivityInput(
        "run-1",
        "tenant-1",
        "manifest-1",
        ArtifactStageState(
            artifact=artifact,
            generation_id="generation-1",
            stage=ArtifactStage.FETCH,
            artifact_revision_id="revision-1",
        ),
    )


def fetch_state(raw: RawDocument) -> SimpleNamespace:
    return SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_source=AsyncMock(
            return_value=SourceRecord(
                id="a",
                source_type="application/vnd.atlassian.jira.issue+json",
                locator="ENG-1",
            )
        ),
        persist_snapshot=AsyncMock(return_value="snapshot://a"),
        complete_stage=AsyncMock(side_effect=lambda request, result: result),
    )
