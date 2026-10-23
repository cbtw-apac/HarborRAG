"""Errors raised while normalizing a Confluence page body."""


class ConfluenceNormalizationError(ValueError):
    """Base error for Confluence page normalization."""


class UnsupportedConfluenceBodyError(ConfluenceNormalizationError):
    """No supported page body representation is available."""


class TableExtractionError(ConfluenceNormalizationError):
    """A source table cannot retain a valid topology."""
