from harborrag_engine.ingestion.chunking.errors import ChunkingError


class TableClassificationError(ChunkingError):
    """Raised when a table cannot be classified deterministically."""


class TableChunkingError(ChunkingError):
    """Raised when a valid table chunk cannot be produced."""


class InvalidTableLocatorError(TableChunkingError):
    """Raised when a table chunk does not resolve to source cells."""


class TableChunkLimitExceededError(TableChunkingError):
    """Raised when a strict caller requests more table chunks than policy permits."""
