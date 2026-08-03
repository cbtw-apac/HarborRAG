class ChunkContractError(ValueError):
    """Base error for canonical chunk contract operations."""


class ChunkValidationError(ChunkContractError):
    """Raised when an explicit chunk validation operation fails."""


class ChunkIdentityError(ChunkContractError):
    """Raised when deterministic identity input cannot be normalized safely."""
