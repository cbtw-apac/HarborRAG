from __future__ import annotations

from harborrag_adapters.models.runtime.errors import (
    ModelErrorCategory,
    normalize_model_exception,
)
from harborrag_core.models import errors as model_errors

_ERROR_TYPES: dict[ModelErrorCategory, type[model_errors.HarborEmbedError]] = {
    ModelErrorCategory.AUTHENTICATION: model_errors.HarborEmbedAuthenticationError,
    ModelErrorCategory.AUTHORIZATION: model_errors.HarborEmbedAuthorizationError,
    ModelErrorCategory.RATE_LIMIT: model_errors.HarborEmbedRateLimitError,
    ModelErrorCategory.TIMEOUT: model_errors.HarborEmbedTimeoutError,
    ModelErrorCategory.CONNECTION: model_errors.HarborEmbedConnectionError,
    ModelErrorCategory.CONTEXT_LENGTH: model_errors.HarborEmbedInputTooLargeError,
    ModelErrorCategory.CONTENT_POLICY: model_errors.HarborEmbedInvalidRequestError,
    ModelErrorCategory.INVALID_REQUEST: model_errors.HarborEmbedInvalidRequestError,
    ModelErrorCategory.STRUCTURED_OUTPUT: model_errors.HarborEmbedInvalidRequestError,
    ModelErrorCategory.PROVIDER: model_errors.HarborEmbedProviderError,
}


def normalize_exception(
    exc: Exception,
    *,
    provider: str | None = None,
    logical_model: str | None = None,
    provider_model: str | None = None,
    deployment: str | None = None,
    request_id: str | None = None,
) -> model_errors.HarborEmbedError:
    """Map an SDK failure into a sanitized embedding error with execution context."""
    return normalize_model_exception(
        exc,
        operation="embed",
        error_base=model_errors.HarborEmbedError,
        error_types=_ERROR_TYPES,
        provider=provider,
        logical_model=logical_model,
        provider_model=provider_model,
        deployment=deployment,
        request_id=request_id,
    )
