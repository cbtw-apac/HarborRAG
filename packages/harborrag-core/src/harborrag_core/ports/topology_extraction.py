"""Provider-neutral boundary for evidence-backed topology extraction."""

from typing import Protocol

from harborrag_core.topology.extraction import (
    ChunkExtractionInput,
    ExtractionOutput,
    ExtractionProfile,
)


class EntityExtractionPort(Protocol):
    async def extract(
        self,
        value: ChunkExtractionInput,
        *,
        profile: ExtractionProfile,
        tenant_id: str,
        document_id: str,
    ) -> ExtractionOutput: ...
