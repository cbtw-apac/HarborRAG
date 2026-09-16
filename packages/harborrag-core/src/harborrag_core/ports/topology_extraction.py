"""Provider-neutral boundary for evidence-backed topology extraction."""

from typing import Protocol

from harborrag_core.models.chat import HarborChatUsage
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


class ExtractionRunProtocol(Protocol):
    @property
    def output(self) -> ExtractionOutput: ...

    @property
    def usage(self) -> HarborChatUsage: ...

    @property
    def provider_calls(self) -> int: ...


class UsageAwareExtractionPort(EntityExtractionPort, Protocol):
    """Extraction that reports billable usage so reservations can settle actuals."""

    async def extract_usage(
        self,
        value: ChunkExtractionInput,
        *,
        profile: ExtractionProfile,
        tenant_id: str,
        document_id: str,
    ) -> ExtractionRunProtocol: ...
