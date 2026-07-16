from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import uuid4

from harborrag_core.models.embed import (
    EmbeddingInput,
    EmbeddingPurpose,
    HarborEmbedMetadata,
    HarborEmbedRequest,
    RawEmbeddingInput,
)
from harborrag_core.models.errors import (
    HarborEmbedConfigurationError,
    HarborEmbedError,
    HarborEmbedInvalidRequestError,
)
from pydantic import ValidationError

from harborrag_adapters.models.common.litellm_backend import build_provider_params
from harborrag_adapters.models.common.transport import reveal_headers
from .configs import HarborEmbedClientConfig, HarborEmbedDefaults, HarborEmbedProviderConfig
from .registry import EmbedProviderRegistry, HarborEmbedProvider
from .validation import default_deployment, validate_embed_request

_RESERVED_PARAMETERS = frozenset(
    {
        "api_base",
        "api_key",
        "api_version",
        "custom_llm_provider",
        "dimensions",
        "encoding_format",
        "extra_headers",
        "input",
        "model",
        "timeout",
        "user",
    }
)


def normalize_embedding_inputs(inputs: RawEmbeddingInput) -> tuple[EmbeddingInput, ...]:
    """Normalize one string, one token array, or a batch into immutable core inputs."""

    if isinstance(inputs, str):
        return (inputs,)
    values = list(cast(Sequence[Any], inputs))
    if not values:
        raise ValueError("embedding inputs cannot be empty")
    if all(isinstance(item, int) and not isinstance(item, bool) for item in values):
        return (tuple(int(item) for item in values),)
    if all(isinstance(item, str) for item in values):
        return tuple(cast(Sequence[str], values))
    normalized: list[EmbeddingInput] = []
    for item in values:
        if isinstance(item, str):
            normalized.append(item)
            continue
        if not isinstance(item, Sequence):
            raise TypeError("embedding inputs must be strings or token arrays")
        tokens = tuple(item)
        if not all(isinstance(token, int) and not isinstance(token, bool) for token in tokens):
            raise TypeError("embedding inputs must be strings or token arrays")
        normalized.append(tuple(cast(tuple[int, ...], tokens)))
    return tuple(normalized)


def prepare_embed_request(
    config: HarborEmbedClientConfig,
    inputs: RawEmbeddingInput | None,
    *,
    request: HarborEmbedRequest | None,
    model: str | None,
    request_kwargs: Mapping[str, Any],
) -> tuple[str, HarborEmbedProviderConfig, HarborEmbedRequest]:
    """Build, default, identify, capability-check, and secure one embed request."""

    try:
        prepared = build_embed_request(
            inputs,
            request=request,
            model=model,
            request_kwargs=request_kwargs,
        )
    except HarborEmbedError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise HarborEmbedInvalidRequestError(
            "invalid embedding request",
            operation="embed",
            logical_model=model,
            original_exception=exc,
            retryable=False,
        ) from exc
    try:
        logical_name, logical = config.model_for(prepared.logical_model)
    except KeyError as exc:
        raise HarborEmbedConfigurationError(
            str(exc),
            operation="embed",
            logical_model=prepared.logical_model,
            original_exception=exc,
        ) from exc
    prepared = apply_embed_defaults(prepared, logical.default_params).model_copy(
        update={"logical_model": logical_name}
    )
    prepared = ensure_embed_request_id(prepared)
    deployment = default_deployment(logical_name, logical)
    return logical_name, deployment, validate_embed_request(prepared, config, deployment)


def build_embed_request(
    inputs: RawEmbeddingInput | None,
    *,
    request: HarborEmbedRequest | None,
    model: str | None,
    request_kwargs: Mapping[str, Any],
) -> HarborEmbedRequest:
    """Build one request from raw inputs or an existing core request."""

    if request is not None and (inputs is not None or request_kwargs):
        raise HarborEmbedInvalidRequestError(
            "request cannot be combined with inputs or keyword parameters",
            operation="embed",
            retryable=False,
        )
    if request is not None:
        return request.model_copy(update={"logical_model": model or request.logical_model})
    if inputs is None:
        raise HarborEmbedInvalidRequestError(
            "embedding inputs are required", operation="embed", retryable=False
        )
    options = dict(request_kwargs)
    metadata = options.pop("metadata", None)
    return HarborEmbedRequest(
        inputs=normalize_embedding_inputs(inputs),
        logical_model=model,
        metadata=(
            metadata
            if isinstance(metadata, HarborEmbedMetadata)
            else HarborEmbedMetadata.model_validate(metadata or {})
        ),
        **options,
    )


def apply_embed_defaults(
    request: HarborEmbedRequest,
    defaults: HarborEmbedDefaults,
) -> HarborEmbedRequest:
    """Apply logical-model defaults and revalidate cross-field combinations."""

    values = request.model_dump(mode="python")
    updates = {
        "dimensions": defaults.dimensions,
        "encoding_format": defaults.encoding_format,
        "purpose": request.metadata.embedding_purpose or defaults.purpose,
        "normalize": defaults.normalize,
        "batch_size": defaults.batch_size,
    }
    for name, value in updates.items():
        if values.get(name) is None:
            values[name] = value
    return HarborEmbedRequest.model_validate(values)


def ensure_embed_request_id(request: HarborEmbedRequest) -> HarborEmbedRequest:
    """Return a request carrying a stable operation identity."""

    if request.metadata.request_id is not None:
        return request
    metadata = request.metadata.model_copy(update={"request_id": str(uuid4())})
    return request.model_copy(update={"metadata": metadata})


def build_litellm_parameters(
    deployment: HarborEmbedProviderConfig,
    request: HarborEmbedRequest,
    *,
    inputs: list[str | list[int]],
    timeout: float,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Translate one validated embedding batch into LiteLLM parameters."""

    conflicts = _RESERVED_PARAMETERS.intersection(request.extra_params)
    if conflicts:
        raise HarborEmbedInvalidRequestError(
            "extra_params cannot replace normalized parameters: " + ", ".join(sorted(conflicts)),
            operation="embed",
            logical_model=request.logical_model,
            request_id=request.metadata.request_id,
            retryable=False,
        )
    descriptor = EmbedProviderRegistry.default().get(deployment.provider)
    model = (
        f"azure/{deployment.deployment_name}"
        if deployment.provider is HarborEmbedProvider.AZURE_OPENAI and deployment.deployment_name
        else deployment.model
    )
    parameters = build_provider_params(
        deployment,
        litellm_provider=descriptor.litellm_provider,
        model=model_override or model,
    )
    deployment_headers = parameters.pop("extra_headers", {})
    request_headers = reveal_headers(request.custom_headers)
    optional: dict[str, Any] = {
        "input": inputs,
        "timeout": timeout,
        "dimensions": request.dimensions,
        "encoding_format": request.encoding_format.value if request.encoding_format else None,
        "user": request.user or request.metadata.user_id,
    }
    parameters.update({name: value for name, value in optional.items() if value is not None})
    parameters.update(_purpose_parameters(deployment.provider, request.purpose))
    parameters.update(request.extra_params)
    headers = {**deployment_headers, **request_headers}
    if headers:
        parameters["extra_headers"] = headers
    return parameters


def litellm_inputs(request: HarborEmbedRequest) -> list[str | list[int]]:
    """Render provider-neutral inputs in LiteLLM's accepted representation."""

    return [item if isinstance(item, str) else list(item) for item in request.inputs]


def effective_batch_size(
    config: HarborEmbedClientConfig,
    deployment: HarborEmbedProviderConfig,
    request: HarborEmbedRequest,
) -> int:
    """Resolve request, client, provider, and capability batch limits."""

    if not deployment.capabilities.batch:
        return 1
    limits = [request.batch_size or config.default_batch_size]
    if deployment.max_batch_size is not None:
        limits.append(deployment.max_batch_size)
    if deployment.capabilities.max_batch_size is not None:
        limits.append(deployment.capabilities.max_batch_size)
    return min(limits)


def _purpose_parameters(
    provider: HarborEmbedProvider,
    purpose: EmbeddingPurpose | None,
) -> dict[str, str]:
    if purpose is None or purpose is EmbeddingPurpose.UNSPECIFIED:
        return {}
    if provider is HarborEmbedProvider.COHERE:
        values = {
            EmbeddingPurpose.QUERY: "search_query",
            EmbeddingPurpose.DOCUMENT: "search_document",
            EmbeddingPurpose.CLASSIFICATION: "classification",
            EmbeddingPurpose.CLUSTERING: "clustering",
        }
        return {"input_type": values[purpose]}
    if provider is HarborEmbedProvider.VOYAGE:
        return {"input_type": purpose.value}
    if provider in {HarborEmbedProvider.GEMINI, HarborEmbedProvider.VERTEX_AI}:
        values = {
            EmbeddingPurpose.QUERY: "RETRIEVAL_QUERY",
            EmbeddingPurpose.DOCUMENT: "RETRIEVAL_DOCUMENT",
            EmbeddingPurpose.CLASSIFICATION: "CLASSIFICATION",
            EmbeddingPurpose.CLUSTERING: "CLUSTERING",
        }
        return {"task_type": values[purpose]}
    return {}
