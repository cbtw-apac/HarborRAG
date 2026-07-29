class ChunkingError(ValueError):
    """Base error for deterministic chunking failures."""


class ChunkValidationError(ChunkingError):
    """Raised when a chunk manifest violates a correctness invariant."""


class ChunkIdentityError(ChunkingError):
    """Raised when deterministic identity input cannot be normalized safely."""


class ChunkHierarchyError(ChunkingError):
    """Raised when chunk ancestry or neighbor references are inconsistent."""


class InvalidChunkingPlanError(ChunkingError):
    """Raised when common chunking-plan limits or identifiers are invalid."""


class UnknownChunkingStrategyError(ChunkingError):
    """Raised when routing selects an unregistered strategy."""


class OversizedChunkError(ChunkingError):
    """Raised when a refinement capability violates the hard maximum."""
