from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from harborrag_core.models.errors import (
    HarborRerankConfigurationError,
    HarborRerankError,
    HarborRerankInvalidRequestError,
)
from harborrag_core.models.rerank import (
    HarborRerankDocument,
    HarborRerankMetadata,
    HarborRerankRequest,
    RawRerankDocument,
)
from pydantic import ValidationError

from harborrag_adapters.models.common.litellm_backend import build_provider_params
from harborrag_adapters.models.common.transport import reveal_headers

from .configs import (
    HarborRerankClientConfig,
    HarborRerankDefaults,
    HarborRerankProviderConfig,
)
from .validation import default_deployment, validate_rerank_request

_RESERVED_PARAMETERS = frozenset(
    {
        "api_base",
        "api_key",
        "api_version",
        "custom_llm_provider",
        "documents",
        "extra_headers",
        "model",
        "query",
        "timeout",
        "top_n",
    }
)


def normalize_rerank_documents(
    documents: Sequence[RawRerankDocument],
) -> tuple[HarborRerankDocument, ...]:
    """Normalize text, structured mappings, and typed documents."""

    normalized: list[HarborRerankDocument] = []
    for item in documents:
        if isinstance(item, HarborRerankDocument):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append(HarborRerankDocument.text(item))
        elif isinstance(item, Mapping):
            normalized.append(HarborRerankDocument(content=dict(item)))
        else:
            raise TypeError("rerank documents must be strings, mappings, or HarborRerankDocument")
    return tuple(normalized)


def prepare_rerank_request(
    config: HarborRerankClientConfig,
    query: str | None,
    documents: Sequence[RawRerankDocument] | None,
    *,
    request: HarborRerankRequest | None,
    model: str | None,
    request_kwargs: Mapping[str, Any],
) -> tuple[str, HarborRerankProviderConfig, HarborRerankRequest]:
    """Build, default, identify, capability-check, and secure one rerank request."""

    try:
        prepared = build_rerank_request(
            query,
            documents,
            request=request,
            model=model,
            request_kwargs=request_kwargs,
        )
    except HarborRerankError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise HarborRerankInvalidRequestError(
            "invalid rerank request",
            operation="rerank",
            logical_model=model,
            original_exception=exc,
            retryable=False,
        ) from exc
    try:
        logical_name, logical = config.model_for(prepared.logical_model)
    except KeyError as exc:
        raise HarborRerankConfigurationError(
            str(exc),
            operation="rerank",
            logical_model=prepared.logical_model,
            original_exception=exc,
        ) from exc
    prepared = apply_rerank_defaults(prepared, logical.default_params).model_copy(
        update={"logical_model": logical_name}
    )
    prepared = ensure_rerank_request_id(prepared)
    deployment = default_deployment(logical_name, logical)
    validate_rerank_request(prepared, config, deployment)
    return logical_name, deployment, prepared


def build_rerank_request(
    query: str | None,
    documents: Sequence[RawRerankDocument] | None,
    *,
    request: HarborRerankRequest | None,
    model: str | None,
    request_kwargs: Mapping[str, Any],
) -> HarborRerankRequest:
    """Build one request without ambiguously mixing input styles."""

    if request is not None and (query is not None or documents is not None or request_kwargs):
        raise HarborRerankInvalidRequestError(
            "request cannot be combined with query, documents, or keyword parameters",
            operation="rerank",
            retryable=False,
        )
    if request is not None:
        return request.model_copy(update={"logical_model": model or request.logical_model})
    if query is None or documents is None:
        raise HarborRerankInvalidRequestError(
            "query and documents are required", operation="rerank", retryable=False
        )
    options = dict(request_kwargs)
    metadata = options.pop("metadata", None)
    return HarborRerankRequest(
        query=query,
        documents=normalize_rerank_documents(documents),
        logical_model=model,
        metadata=(
            metadata
            if isinstance(metadata, HarborRerankMetadata)
            else HarborRerankMetadata.model_validate(metadata or {})
        ),
        **options,
    )


def apply_rerank_defaults(
    request: HarborRerankRequest,
    defaults: HarborRerankDefaults,
) -> HarborRerankRequest:
    """Apply logical defaults without overriding explicit request values."""

    values = request.model_dump(mode="python")
    updates: dict[str, Any] = {
        "top_n": defaults.top_n,
        "return_documents": defaults.return_documents,
        "max_chunks_per_doc": defaults.max_chunks_per_doc,
        "max_tokens_per_doc": defaults.max_tokens_per_doc,
        "instruction": defaults.instruction,
    }
    for name, value in updates.items():
        if values.get(name) is None:
            values[name] = value
    if values["top_n"] is not None:
        values["top_n"] = min(values["top_n"], len(request.documents))
    return HarborRerankRequest.model_validate(values)


def ensure_rerank_request_id(request: HarborRerankRequest) -> HarborRerankRequest:
    """Return a request carrying a stable operation identity."""

    if request.metadata.request_id is not None:
        return request
    metadata = request.metadata.model_copy(update={"request_id": str(uuid4())})
    return request.model_copy(update={"metadata": metadata})


def build_litellm_parameters(
    deployment: HarborRerankProviderConfig,
    request: HarborRerankRequest,
    *,
    timeout: float,
    model_override: str | None = None,
    litellm_provider: str | None = None,
) -> dict[str, Any]:
    """Translate one provider-neutral rerank request into LiteLLM parameters."""

    conflicts = _RESERVED_PARAMETERS.intersection(request.extra_params)
    if conflicts:
        raise HarborRerankInvalidRequestError(
            "extra_params cannot replace normalized parameters: " + ", ".join(sorted(conflicts)),
            operation="rerank",
            logical_model=request.logical_model,
            request_id=request.metadata.request_id,
            retryable=False,
        )
    parameters = build_provider_params(
        deployment,
        litellm_provider=litellm_provider,
        model=model_override,
    )
    deployment_headers = parameters.pop("extra_headers", {})
    request_headers = reveal_headers(request.custom_headers)
    optional: dict[str, Any] = {
        "query": request.query,
        "documents": [document.content for document in request.documents],
        "top_n": request.top_n,
        "rank_fields": list(request.rank_fields) or None,
        "return_documents": request.return_documents,
        "max_chunks_per_doc": request.max_chunks_per_doc,
        "max_tokens_per_doc": request.max_tokens_per_doc,
        "instruction": request.instruction,
        "timeout": timeout,
    }
    parameters.update({name: value for name, value in optional.items() if value is not None})
    parameters.update(request.extra_params)
    headers = {**deployment_headers, **request_headers}
    if headers:
        parameters["extra_headers"] = headers
    return parameters
