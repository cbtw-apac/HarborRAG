from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from harborrag_core.models import errors as model_errors


class HarborNoHealthyDeploymentError(model_errors.HarborModelError):
    """Report that routing exhausted every healthy deployment and fallback."""


class ModelErrorCategory(StrEnum):
    """Classify provider failures independently of a model family."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    CONTEXT_LENGTH = "context_length"
    CONTENT_POLICY = "content_policy"
    INVALID_REQUEST = "invalid_request"
    STRUCTURED_OUTPUT = "structured_output"
    PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class ModelErrorDetails:
    """Carry sanitized details extracted from a provider exception."""

    category: ModelErrorCategory
    message: str
    retryable: bool | None
    status_code: int | None
    provider: str | None
    provider_request_id: str | None


def normalize_model_exception[E: model_errors.HarborModelError](
    exc: Exception,
    *,
    operation: str,
    error_base: type[E],
    error_types: Mapping[ModelErrorCategory, type[E]],
    provider: str | None = None,
    logical_model: str | None = None,
    provider_model: str | None = None,
    deployment: str | None = None,
    request_id: str | None = None,
) -> E:
    """Map an SDK exception into a family error using one shared classifier."""
    if isinstance(exc, error_base):
        return exc.enrich(
            operation=operation,
            provider=provider,
            logical_model=logical_model,
            provider_model=provider_model,
            deployment=deployment,
            request_id=request_id,
        )
    details = classify_model_exception(exc, provider=provider)
    error_type = error_types.get(details.category, error_types[ModelErrorCategory.PROVIDER])
    return error_type(
        details.message,
        operation=operation,
        provider=details.provider,
        logical_model=logical_model,
        provider_model=provider_model,
        deployment=deployment,
        retryable=details.retryable,
        status_code=details.status_code,
        request_id=request_id,
        provider_request_id=details.provider_request_id,
        original_exception=exc,
    )


def classify_model_exception(exc: Exception, *, provider: str | None = None) -> ModelErrorDetails:
    """Classify LiteLLM and common transport failures without exposing raw payloads."""
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        status_code = None
    category = ModelErrorCategory.PROVIDER
    retryable: bool | None = None
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        category = ModelErrorCategory.TIMEOUT
        retryable = True
    elif isinstance(exc, ConnectionError):
        category = ModelErrorCategory.CONNECTION
        retryable = True

    try:
        import litellm

        mappings = (
            ("AuthenticationError", ModelErrorCategory.AUTHENTICATION),
            ("PermissionDeniedError", ModelErrorCategory.AUTHORIZATION),
            ("RateLimitError", ModelErrorCategory.RATE_LIMIT),
            ("Timeout", ModelErrorCategory.TIMEOUT),
            ("APIConnectionError", ModelErrorCategory.CONNECTION),
            ("ContextWindowExceededError", ModelErrorCategory.CONTEXT_LENGTH),
            ("ContentPolicyViolationError", ModelErrorCategory.CONTENT_POLICY),
            ("UnsupportedParamsError", ModelErrorCategory.INVALID_REQUEST),
            ("BadRequestError", ModelErrorCategory.INVALID_REQUEST),
            ("NotFoundError", ModelErrorCategory.INVALID_REQUEST),
            ("JSONSchemaValidationError", ModelErrorCategory.STRUCTURED_OUTPUT),
        )
        for name, candidate in mappings:
            source = getattr(litellm, name, None)
            if isinstance(source, type) and isinstance(exc, source):
                category = candidate
                break
        transient_types = tuple(
            source
            for name in ("InternalServerError", "ServiceUnavailableError", "APIError")
            if isinstance((source := getattr(litellm, name, None)), type)
        )
        if transient_types and isinstance(exc, transient_types):
            category = ModelErrorCategory.PROVIDER
            retryable = True
    except ImportError:
        pass

    if status_code in {408, 409, 425, 429} or (status_code is not None and status_code >= 500):
        retryable = True
    elif status_code is not None and 400 <= status_code < 500:
        retryable = False
    return ModelErrorDetails(
        category=category,
        message=safe_provider_error_message(exc),
        retryable=retryable,
        status_code=status_code,
        provider=getattr(exc, "llm_provider", None) or provider,
        provider_request_id=_provider_request_id(exc),
    )


def safe_provider_error_message(exc: Exception) -> str:
    """Return a stable provider error summary without raw prompt or payload content."""

    error_name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return f"{error_name}: provider request failed with status {status_code}"
    return f"{error_name}: provider request failed"


def _provider_request_id(exc: Exception) -> str | None:
    """Extract a provider request ID from an exception response, when available."""
    response = getattr(exc, "response", None)
    headers: Any = getattr(response, "headers", None) if response is not None else None
    if isinstance(headers, Mapping):
        value = headers.get("x-request-id") or headers.get("request-id")
        return str(value) if value else None
    return None
