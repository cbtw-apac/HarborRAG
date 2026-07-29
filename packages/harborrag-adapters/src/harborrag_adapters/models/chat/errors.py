from __future__ import annotations

from harborrag_adapters.models.runtime.errors import (
    ModelErrorCategory,
    build_exception_normalizer,
)
from harborrag_core.models import errors as model_errors

# Chat is the only family that models every category distinctly.
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

normalize_exception = build_exception_normalizer(
    operation="chat",
    error_base=model_errors.HarborChatError,
    error_types=_ERROR_TYPES,
)
