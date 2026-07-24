"""Runtime-owned dependency assembly and durable ingestion-state boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.parsers.engine import HarborParser
from harborrag_core.domain.document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ports.runtime import AsyncLifecyclePort, RuntimeObserverPort
from harborrag_engine.ingestion.base import BaseChunker, BaseDocumentNormalizer
from harborrag_engine.ingestion.chunking.manifest import ChunkPersistenceService
from harborrag_engine.ingestion.chunking.schemas import ChunkingResult
from harborrag_engine.ingestion.indexing.pipeline import IndexingService
from harborrag_engine.ingestion.indexing.schemas import IndexingRequest, IndexingResult
from harborrag_runtime.errors import RuntimeConfigurationError
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    DiscoveryInput,
    DiscoveryResult,
    ReconciliationInput,
    ReconciliationResult,
    ResolutionDecision,
    ResolutionReceipt,
)


class RuntimeIngestionState(Protocol):
    """Durable bridge to authoritative engine manifests and repositories.

    Implementations store large stage values outside Temporal history and make
    every completion method idempotent. The default runtime implementation uses
    state and object repositories; deployments can replace it at composition.
    """

    async def initialize_run(self, request: DiscoveryInput) -> None: ...

    async def persist_discovered(
        self,
        request: DiscoveryInput,
        source: SourceRecord,
    ) -> ArtifactReference: ...

    async def discovery_progress(
        self,
        request: DiscoveryInput,
    ) -> DiscoveryResult | None: ...

    async def save_discovery_progress(
        self,
        request: DiscoveryInput,
        artifacts: tuple[ArtifactReference, ...],
        next_cursor: str | None,
        *,
        done: bool,
    ) -> str: ...

    async def completed_stage(
        self,
        request: ArtifactActivityInput,
        stage: ArtifactStage,
    ) -> ArtifactActivityResult | None: ...

    async def complete_stage(
        self,
        request: ArtifactActivityInput,
        result: ArtifactActivityResult,
    ) -> ArtifactActivityResult: ...

    async def preflight(self, request: ArtifactActivityInput) -> ArtifactActivityResult: ...

    async def load_source(self, source_ref: str) -> SourceRecord: ...

    async def persist_snapshot(
        self,
        request: ArtifactActivityInput,
        document: RawDocument,
    ) -> str: ...

    async def load_snapshot(self, snapshot_ref: str) -> RawDocument: ...

    async def persist_parsed_document(
        self,
        request: ArtifactActivityInput,
        parsed: ParsedDocument,
        document: Document,
    ) -> str: ...

    async def load_parsed_document(self, parsed_document_ref: str) -> Document: ...

    async def persist_chunking_result(
        self,
        request: ArtifactActivityInput,
        result: ChunkingResult,
    ) -> str: ...

    async def load_chunking_result(self, chunking_result_ref: str) -> ChunkingResult: ...

    async def indexing_request(
        self,
        request: ArtifactActivityInput,
        chunking: ChunkingResult,
    ) -> IndexingRequest: ...

    async def persist_indexing_result(
        self,
        request: ArtifactActivityInput,
        result: IndexingResult,
    ) -> str: ...

    async def validate(self, request: ArtifactActivityInput) -> ArtifactActivityResult: ...

    async def finalize(self, request: ArtifactActivityInput) -> ArtifactActivityResult: ...

    async def reconcile(self, request: ReconciliationInput) -> ReconciliationResult: ...

    async def apply_resolution(
        self,
        request: ArtifactActivityInput,
        decision: ResolutionDecision,
    ) -> ResolutionReceipt: ...

    async def health(self) -> Mapping[str, object]: ...


class NullRuntimeObserver:
    def record(self, event: str, attributes: Mapping[str, str | int | float]) -> None:
        return None


@dataclass(slots=True)
class RuntimeDependencies:
    """One shared, explicitly owned service graph per worker process."""

    connectors: Mapping[str, BaseConnector | HarborConnector]
    parser: HarborParser
    normalizer: BaseDocumentNormalizer
    chunker: BaseChunker
    chunk_persistence: ChunkPersistenceService
    indexer: IndexingService
    state: RuntimeIngestionState
    resources: tuple[AsyncLifecyclePort, ...] = ()
    observer: RuntimeObserverPort = field(default_factory=NullRuntimeObserver)
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.connectors:
            raise RuntimeConfigurationError("At least one ingestion connector is required")
        self.connectors = MappingProxyType(dict(self.connectors))

    def connector(self, name: str) -> BaseConnector | HarborConnector:
        try:
            return self.connectors[name]
        except KeyError as exc:
            raise RuntimeConfigurationError(f"Unknown runtime connector: {name!r}") from exc

    async def start(self) -> None:
        """Start resources and connector sessions exactly once."""

        async with self._lock:
            if self._started:
                return
            started: list[AsyncLifecyclePort] = []
            connected: list[BaseConnector | HarborConnector] = []
            try:
                for resource in self.resources:
                    await resource.start()
                    started.append(resource)
                for connector in self.connectors.values():
                    await asyncio.to_thread(connector.connect)
                    connected.append(connector)
            except BaseException as start_error:
                cleanup_errors: list[BaseException] = []
                for connector in reversed(connected):
                    try:
                        await asyncio.to_thread(connector.close)
                    except BaseException as exc:  # keep rolling back the graph
                        cleanup_errors.append(exc)
                for resource in reversed(started):
                    try:
                        await resource.close()
                    except BaseException as exc:  # keep rolling back the graph
                        cleanup_errors.append(exc)
                if cleanup_errors:
                    raise BaseExceptionGroup(
                        "runtime dependency startup and rollback failed",
                        [start_error, *cleanup_errors],
                    )
                raise
            self._started = True
            self._closed = False

    async def close(self) -> None:
        """Close connectors and resources in reverse ownership order."""

        async with self._lock:
            if self._closed:
                return
            errors: list[BaseException] = []
            for connector in reversed(tuple(self.connectors.values())):
                try:
                    await asyncio.to_thread(connector.close)
                except BaseException as exc:  # close the rest of the owned graph
                    errors.append(exc)
            for resource in reversed(self.resources):
                try:
                    await resource.close()
                except BaseException as exc:  # close the rest of the owned graph
                    errors.append(exc)
            self._closed = True
            self._started = False
            if errors:
                raise BaseExceptionGroup("runtime dependency shutdown failed", errors)

    async def health(self) -> dict[str, object]:
        state_health = await self.state.health()
        return {
            "ready": self._started and not self._closed,
            "connectors": tuple(self.connectors),
            "state": dict(state_health),
        }
