"""Typed internal contract shared by repository-backed ingestion state mixins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from harborrag_adapters.repositories.state.base import (
    HarborStateBackend,
    HarborStateStore,
)
from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.ids import WorkflowId
from harborrag_core.schemas.state import WorkflowState
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion.indexing.activation import (
    IndexGenerationActivationService,
)
from harborrag_engine.ingestion.indexing.config import IndexingConfig
from harborrag_engine.ingestion.indexing.schemas import IndexingResult

from .artifact_objects import IngestionObjectRepository
from .schemas import (
    ArtifactActivityInput,
    ArtifactReference,
    ArtifactStage,
    DiscoveryInput,
)


class IngestionStateMixinBase(ABC):
    """Declare collaborators supplied by sibling mixins and the concrete state."""

    _backend: HarborStateBackend
    _states: HarborStateStore
    _objects: IngestionObjectRepository
    _indexing_config: IndexingConfig
    _activator: IndexGenerationActivationService | None
    _chunking_configuration_version: str

    @abstractmethod
    async def _active_state(
        self,
        request: ArtifactActivityInput,
    ) -> WorkflowState | None:
        raise NotImplementedError

    @abstractmethod
    async def _upsert_state(
        self,
        workflow_id: WorkflowId,
        tenant_id: str,
        context: StorageOperationContext,
        *,
        current_step: str,
        payload: dict[str, Any],
    ) -> WorkflowState:
        raise NotImplementedError

    @abstractmethod
    async def _create_idempotently(
        self,
        state: WorkflowState,
        context: StorageOperationContext,
    ) -> WorkflowState:
        raise NotImplementedError

    @abstractmethod
    async def _reserve_generation(
        self,
        request: ArtifactActivityInput,
        revision_id: str,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def _begin_promotion(
        self,
        request: ArtifactActivityInput,
        indexing: IndexingResult,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def _finish_promotion(self, request: ArtifactActivityInput) -> bool:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def _revision_id(artifact: ArtifactReference, source: SourceRecord) -> str:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def _context(tenant_id: str, run_id: str) -> StorageOperationContext:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def _workflow_id(*parts: str) -> WorkflowId:
        raise NotImplementedError

    @abstractmethod
    def _discovery_id(self, request: DiscoveryInput) -> WorkflowId:
        raise NotImplementedError

    @abstractmethod
    def _stage_id(
        self,
        request: ArtifactActivityInput,
        stage: ArtifactStage,
    ) -> WorkflowId:
        raise NotImplementedError

    @abstractmethod
    def _active_id(self, request: ArtifactActivityInput) -> WorkflowId:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def _artifact_key(run_id: str, artifact_id: str, kind: str) -> str:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def _optional_text(value: Any) -> str | None:
        raise NotImplementedError
