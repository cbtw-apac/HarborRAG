from __future__ import annotations

from harborrag_adapters.models.common.errors import (
    ModelErrorCategory,
    normalize_model_exception,
)
from harborrag_core.models import errors as model_errors

_ERROR_TYPES: dict[ModelErrorCategory, type[model_errors.HarborRerankError]] = {
    ModelErrorCategory.AUTHENTICATION: model_errors.HarborRerankAuthenticationError,
    ModelErrorCategory.AUTHORIZATION: model_errors.HarborRerankAuthorizationError,
    ModelErrorCategory.RATE_LIMIT: model_errors.HarborRerankRateLimitError,
    ModelErrorCategory.TIMEOUT: model_errors.HarborRerankTimeoutError,
    ModelErrorCategory.CONNECTION: model_errors.HarborRerankConnectionError,
    ModelErrorCategory.CONTEXT_LENGTH: model_errors.HarborRerankInvalidRequestError,
    ModelErrorCategory.CONTENT_POLICY: model_errors.HarborRerankInvalidRequestError,
    ModelErrorCategory.INVALID_REQUEST: model_errors.HarborRerankInvalidRequestError,
    ModelErrorCategory.STRUCTURED_OUTPUT: model_errors.HarborRerankInvalidRequestError,
    ModelErrorCategory.PROVIDER: model_errors.HarborRerankProviderError,
}


def normalize_exception(
    exc: Exception,
    *,
    provider: str | None = None,
    logical_model: str | None = None,
    provider_model: str | None = None,
    deployment: str | None = None,
    request_id: str | None = None,
) -> model_errors.HarborRerankError:
    """Map an SDK failure into a sanitized reranking error with execution context."""
    return normalize_model_exception(
        exc,
        operation="rerank",
        error_base=model_errors.HarborRerankError,
        error_types=_ERROR_TYPES,
        provider=provider,
        logical_model=logical_model,
        provider_model=provider_model,
        deployment=deployment,
        request_id=request_id,
    )
