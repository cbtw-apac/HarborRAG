from __future__ import annotations

from harborrag_adapters.models.runtime.errors import (
    ModelErrorCategory,
    build_exception_normalizer,
)
from harborrag_core.models import errors as model_errors

# Embeddings have no content-moderation or structured-output failure mode of their
# own, so those two categories intentionally collapse onto InvalidRequest rather
# than inventing embed-specific types. CONTEXT_LENGTH gets the more specific
# InputTooLarge, which is itself a subclass of InvalidRequest.
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

normalize_exception = build_exception_normalizer(
    operation="embed",
    error_base=model_errors.HarborEmbedError,
    error_types=_ERROR_TYPES,
)
