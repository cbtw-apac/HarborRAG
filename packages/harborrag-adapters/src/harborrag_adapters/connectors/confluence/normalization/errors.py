class ConfluenceNormalizationError(ValueError):
    """Base error for pure Confluence page normalization."""


class UnsupportedConfluenceBodyError(ConfluenceNormalizationError):
    """Raised when no supported page body representation is available."""


class TableExtractionError(ConfluenceNormalizationError):
    """Raised when a source table cannot retain a valid topology."""
