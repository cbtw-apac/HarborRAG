class IndexingError(RuntimeError):
    """Base error for provider-independent indexing behavior."""


class ChunkDiffError(IndexingError):
    """Raised when chunk manifests cannot be compared safely."""


class EmbeddingResultMismatchError(IndexingError):
    """Raised when provider embeddings do not satisfy a planned batch."""


class VectorIndexValidationError(IndexingError):
    """Raised when vector read-after-write validation fails."""


class GraphIndexValidationError(IndexingError):
    """Raised when graph read-after-write validation fails."""
