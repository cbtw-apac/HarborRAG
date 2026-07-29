from __future__ import annotations

from harborrag_adapters.models.runtime.errors import (
    ModelErrorCategory,
    build_exception_normalizer,
)
from harborrag_core.models import errors as model_errors

# Reranking has no dedicated context-length, moderation, or structured-output type:
# an oversized document set, a moderation refusal, and a schema failure are all
# request-shape problems from a rerank caller's point of view, so all three
# categories intentionally collapse onto InvalidRequest.
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

normalize_exception = build_exception_normalizer(
    operation="rerank",
    error_base=model_errors.HarborRerankError,
    error_types=_ERROR_TYPES,
)
