"""Chat failures that answer with their own code from the error catalogue."""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborUnavailableError, HarborValidationError


class UnknownReferenceError(HarborValidationError):
    """A project or connection the request names does not exist in the tenant."""

    error_code = "reference_not_found"


class ConnectionOutOfScopeError(HarborValidationError):
    """A requested connection sits outside the session's or the question's projects."""

    error_code = "connection_out_of_scope"


class ChatModelUnavailableError(HarborUnavailableError):
    """No chat model answered: none is configured, or the provider is down."""

    error_code = "llm_unavailable"


__all__ = ["ChatModelUnavailableError", "ConnectionOutOfScopeError", "UnknownReferenceError"]
