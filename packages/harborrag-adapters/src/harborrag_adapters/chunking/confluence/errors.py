class ConfluenceNormalizationError(ValueError):
    """Base error for pure Confluence page normalization."""


class UnsupportedConfluenceBodyError(ConfluenceNormalizationError):
    """Raised when no supported page body representation is available."""


class ConfluenceMacroParsingError(ConfluenceNormalizationError):
    """Raised when a known macro has invalid structural state."""


class TableExtractionError(ConfluenceNormalizationError):
    """Raised when a source table cannot retain a valid topology."""
