"""Kit-level errors that connectors catch and turn into item failures."""

from __future__ import annotations

from harborrag_core.ingestion.errors import IngestionDomainError
from harborrag_core.sources import ConfigError, Transient


class ResponseTooLarge(IngestionDomainError):
    """A response or blob exceeded its byte cap; retrying returns the same bytes."""

    retryable = False

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"response exceeds the {limit}-byte limit")


class MalformedResponse(Transient):
    """The body could not be decoded; a batch reader bisects to find the culprit."""


class UnexpectedStatus(ConfigError):
    """A client error the kit has no specific meaning for (400, 405, 422, ...).

    ``status_code`` and ``detail`` let a connector recognise a vendor quirk,
    such as a Data Center pagination 400, and recover.
    """

    def __init__(self, status_code: int, message: str, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)
