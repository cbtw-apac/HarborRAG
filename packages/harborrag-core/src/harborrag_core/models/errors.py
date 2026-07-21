from __future__ import annotations

from typing import Any, Self

from harborrag_core.errors import HarborError


class HarborModelError(HarborError):
    """Base error carrying sanitized diagnostics for any model operation."""

    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        provider: str | None = None,
        logical_model: str | None = None,
        provider_model: str | None = None,
        deployment: str | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        provider_request_id: str | None = None,
        original_exception: BaseException | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store provider-neutral diagnostics without retaining request payloads."""
        super().__init__(message)
        self.operation = operation
        self.provider = provider
        self.logical_model = logical_model
        self.provider_model = provider_model
        self.deployment = deployment
        self.retryable = self.default_retryable if retryable is None else retryable
        self.status_code = status_code
        self.request_id = request_id
        self.provider_request_id = provider_request_id
        self.original_exception = original_exception
        self.metadata = metadata or {}

    def enrich(
        self,
        *,
        operation: str | None = None,
        provider: str | None = None,
        logical_model: str | None = None,
        provider_model: str | None = None,
        deployment: str | None = None,
        request_id: str | None = None,
    ) -> Self:
        """Fill missing execution context while preserving supplied details."""
        values = {
            "operation": operation,
            "provider": provider,
            "logical_model": logical_model,
            "provider_model": provider_model,
            "deployment": deployment,
            "request_id": request_id,
        }
        for name, value in values.items():
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
