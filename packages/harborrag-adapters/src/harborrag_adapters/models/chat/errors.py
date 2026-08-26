from __future__ import annotations

from harborrag_adapters.models.runtime.errors import (
    ModelErrorCategory,
    normalize_model_exception,
)
from harborrag_core.models import errors as model_errors

_ERROR_TYPES: dict[ModelErrorCategory, type[model_errors.HarborChatError]] = {
    ModelErrorCategory.AUTHENTICATION: model_errors.HarborChatAuthenticationError,
    ModelErrorCategory.AUTHORIZATION: model_errors.HarborChatAuthorizationError,
    ModelErrorCategory.RATE_LIMIT: model_errors.HarborChatRateLimitError,
    ModelErrorCategory.TIMEOUT: model_errors.HarborChatTimeoutError,
    ModelErrorCategory.CONNECTION: model_errors.HarborChatConnectionError,
    ModelErrorCategory.CONTEXT_LENGTH: model_errors.HarborChatContextLengthError,
    ModelErrorCategory.CONTENT_POLICY: model_errors.HarborChatContentPolicyError,
    ModelErrorCategory.INVALID_REQUEST: model_errors.HarborChatInvalidRequestError,
    ModelErrorCategory.STRUCTURED_OUTPUT: model_errors.HarborChatStructuredOutputError,
    ModelErrorCategory.PROVIDER: model_errors.HarborChatProviderError,
}


def normalize_exception(
    exc: Exception,
    *,
    provider: str | None = None,
    logical_model: str | None = None,
    provider_model: str | None = None,
    deployment: str | None = None,
    request_id: str | None = None,
) -> model_errors.HarborChatError:
    """Map an SDK failure into a sanitized chat error with execution context."""
    return normalize_model_exception(
        exc,
        operation="chat",
        error_base=model_errors.HarborChatError,
        error_types=_ERROR_TYPES,
        provider=provider,
        logical_model=logical_model,
        provider_model=provider_model,
        deployment=deployment,
        request_id=request_id,
    )
