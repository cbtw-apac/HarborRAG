"""Repository-backed, restart-safe state for Temporal ingestion activities."""

from __future__ import annotations

from harborrag_adapters.repositories.state.base import (
    HarborStateBackend,
    HarborStateStore,
)
from harborrag_engine.ingestion.indexing.activation import (
    IndexGenerationActivationService,
)
from harborrag_engine.ingestion.indexing.config import IndexingConfig

from .artifact_lifecycle import ArtifactLifecycleMixin
from .artifact_objects import IngestionObjectRepository
from .artifact_payloads import ArtifactPayloadMixin
from .discovery_state import DiscoveryStateMixin
from .generation_promotion import GenerationPromotionMixin
from .state_storage import IngestionStateStorageMixin


class RepositoryRuntimeIngestionState(
    DiscoveryStateMixin,
    ArtifactPayloadMixin,
    ArtifactLifecycleMixin,
    GenerationPromotionMixin,
    IngestionStateStorageMixin,
):
    """Coordinate idempotent stages using state and object repositories."""

    def __init__(
        self,
        state_backend: HarborStateBackend,
        objects: IngestionObjectRepository,
        indexing_config: IndexingConfig,
        activator: IndexGenerationActivationService | None = None,
        *,
        chunking_configuration_version: str = "1",
    ) -> None:
        self._backend = state_backend
        self._states: HarborStateStore = state_backend.state
        self._objects = objects
        self._indexing_config = indexing_config
        self._activator = activator
        if not chunking_configuration_version.strip():
            raise ValueError("chunking_configuration_version must be non-empty")
        self._chunking_configuration_version = chunking_configuration_version
