from harborrag_core.chunking.errors import ChunkIdentityError as ChunkIdentityError
from harborrag_core.ingestion import ChunkValidationError as IngestionChunkValidationError


class ChunkingError(ValueError):
    """Base error for deterministic chunking failures."""


class ChunkValidationError(ChunkingError, IngestionChunkValidationError):
    """Raised when a chunk manifest violates a correctness invariant."""


class ChunkHierarchyError(ChunkingError):
    """Raised when chunk ancestry or neighbor references are inconsistent."""


class InvalidChunkingPlanError(ChunkingError):
    """Raised when common chunking-plan limits or identifiers are invalid."""


class UnknownChunkingStrategyError(ChunkingError):
    """Raised when routing selects an unregistered strategy."""


class OversizedChunkError(ChunkingError):
    """Raised when a refinement capability violates the hard maximum."""
