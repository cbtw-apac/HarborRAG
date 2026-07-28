class ChunkingError(ValueError):
    """Base error for deterministic chunking failures."""


class ChunkValidationError(ChunkingError):
    """Raised when a chunk manifest violates a correctness invariant."""


class UnknownChunkingStrategyError(ChunkingError):
    """Raised when routing selects an unregistered strategy."""


class OversizedChunkError(ChunkingError):
    """Raised when a refinement capability violates the hard maximum."""
