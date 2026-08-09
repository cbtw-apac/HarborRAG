from __future__ import annotations

from typing import Any, Self, TypedDict, Unpack

from harborrag_core.contracts.errors import HarborError
from harborrag_core.security.field_names import canonical_field_name, canonical_field_tokens
from harborrag_core.security.redaction import redact_secrets

_SENSITIVE_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
    }
)


class ModelErrorDetails(TypedDict, total=False):
    operation: str | None
    provider: str | None
    logical_model: str | None
    provider_model: str | None
    deployment: str | None
    retryable: bool | None
    status_code: int | None
    request_id: str | None
    provider_request_id: str | None
    original_exception: BaseException | None
    metadata: dict[str, Any] | None


class ModelErrorEnrichment(TypedDict, total=False):
    operation: str | None
    provider: str | None
    logical_model: str | None
    provider_model: str | None
    deployment: str | None
    request_id: str | None


def _sanitize_diagnostic(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = canonical_field_name(key)
            tokens = canonical_field_tokens(key)
            sanitized[key] = (
                "<redacted>"
                if normalized in _SENSITIVE_KEYS or tokens & _SENSITIVE_KEYS
                else _sanitize_diagnostic(item)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_diagnostic(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(str(value))


class HarborModelError(HarborError):
    """Base error carrying sanitized diagnostics for any model operation."""

    default_retryable = False

    def __init__(
        self,
        message: str,
        **details: Unpack[ModelErrorDetails],
    ) -> None:
        """Store provider-neutral diagnostics without retaining request payloads."""
        super().__init__(redact_secrets(message))
        self.operation = details.get("operation")
        self.provider = details.get("provider")
        self.logical_model = details.get("logical_model")
        self.provider_model = details.get("provider_model")
        self.deployment = details.get("deployment")
        retryable = details.get("retryable")
        self.retryable = self.default_retryable if retryable is None else retryable
        self.status_code = details.get("status_code")
        self.request_id = details.get("request_id")
        self.provider_request_id = details.get("provider_request_id")
        self.original_exception = details.get("original_exception")
        self.metadata = _sanitize_diagnostic(details.get("metadata") or {})

    def enrich(
        self,
        **details: Unpack[ModelErrorEnrichment],
    ) -> Self:
        """Fill missing execution context while preserving supplied details."""
        for name, value in details.items():
            if getattr(self, name) is None and value is not None:
                setattr(self, name, value)
        return self

    def to_dict(self) -> dict[str, Any]:
        """Render sanitized diagnostics as a JSON-serializable mapping."""
        return {
            "type": type(self).__name__,
            "message": str(self),
            "operation": self.operation,
            "provider": self.provider,
            "logical_model": self.logical_model,
            "provider_model": self.provider_model,
            "deployment": self.deployment,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "request_id": self.request_id,
            "provider_request_id": self.provider_request_id,
            "metadata": self.metadata,
        }


class HarborChatError(HarborModelError):
    """Base error for chat operations."""


class HarborChatAuthenticationError(HarborChatError):
    """Report chat authentication failures."""


class HarborChatAuthorizationError(HarborChatError):
    """Report chat authorization failures."""


class HarborChatRateLimitError(HarborChatError):
    """Report chat rate-limit failures."""

    default_retryable = True


class HarborChatTimeoutError(HarborChatError):
    """Report chat timeout failures."""

    default_retryable = True


class HarborChatConnectionError(HarborChatError):
    """Report chat connection failures."""

    default_retryable = True


class HarborChatInvalidRequestError(HarborChatError):
    """Report invalid chat requests."""


class HarborChatContextLengthError(HarborChatInvalidRequestError):
    """Report chat context-length failures."""


class HarborChatContentPolicyError(HarborChatInvalidRequestError):
    """Report chat content-policy failures."""


class HarborChatProviderError(HarborChatError):
    """Report chat provider failures."""


class HarborChatCapabilityError(HarborChatProviderError):
    """Report chat features unsupported by a candidate deployment."""

    default_retryable = True


class HarborChatConfigurationError(HarborChatError):
    """Report chat configuration failures."""


class HarborChatBudgetExceededError(HarborChatError):
    """Report chat token-budget failures."""


class HarborChatStructuredOutputError(HarborChatError):
    """Report chat structured-output failures."""


class HarborChatCancelledError(HarborChatError):
    """Report cancelled chat operations."""


class HarborEmbedError(HarborModelError):
    """Base error for embedding operations."""


class HarborEmbedAuthenticationError(HarborEmbedError):
    """Report embedding authentication failures."""


class HarborEmbedAuthorizationError(HarborEmbedError):
    """Report embedding authorization failures."""


class HarborEmbedRateLimitError(HarborEmbedError):
    """Report embedding rate-limit failures."""

    default_retryable = True


class HarborEmbedTimeoutError(HarborEmbedError):
    """Report embedding timeout failures."""

    default_retryable = True


class HarborEmbedConnectionError(HarborEmbedError):
    """Report embedding connection failures."""

    default_retryable = True


class HarborEmbedInvalidRequestError(HarborEmbedError):
    """Report invalid embedding requests."""


class HarborEmbedInputTooLargeError(HarborEmbedInvalidRequestError):
    """Report embedding inputs that exceed model limits."""


class HarborEmbedProviderError(HarborEmbedError):
    """Report embedding provider failures."""


class HarborEmbedConfigurationError(HarborEmbedError):
    """Report embedding configuration failures."""


class HarborEmbedCapabilityError(HarborEmbedProviderError):
    """Report unsupported embedding capabilities."""

    default_retryable = True


class HarborEmbedMalformedResponseError(HarborEmbedError):
    """Report malformed embedding provider responses."""


class HarborEmbedPartialBatchError(HarborEmbedProviderError):
    """Report a terminal batch failure after earlier embedding batches completed."""


class HarborEmbedCancelledError(HarborEmbedError):
    """Report cancelled embedding operations."""


class HarborRerankError(HarborModelError):
    """Base error for reranking operations."""


class HarborRerankAuthenticationError(HarborRerankError):
    """Report reranking authentication failures."""


class HarborRerankAuthorizationError(HarborRerankError):
    """Report reranking authorization failures."""


class HarborRerankRateLimitError(HarborRerankError):
    """Report reranking rate-limit failures."""

    default_retryable = True


class HarborRerankTimeoutError(HarborRerankError):
    """Report reranking timeout failures."""

    default_retryable = True


class HarborRerankConnectionError(HarborRerankError):
    """Report reranking connection failures."""

    default_retryable = True


class HarborRerankInvalidRequestError(HarborRerankError):
    """Report invalid reranking requests."""


class HarborRerankProviderError(HarborRerankError):
    """Report reranking provider failures."""


class HarborRerankCapabilityError(HarborRerankProviderError):
    """Report unsupported reranking capabilities."""

    default_retryable = True


class HarborRerankConfigurationError(HarborRerankError):
    """Report reranking configuration failures."""


class HarborRerankMalformedResponseError(HarborRerankError):
    """Report malformed reranking provider responses."""


class HarborRerankCancelledError(HarborRerankError):
    """Report cancelled reranking operations."""
