class ChunkContractError(ValueError):
    """Base error for canonical chunk contract operations."""


class ChunkIdentityError(ChunkContractError):
    """Raised when deterministic identity input cannot be normalized safely."""
