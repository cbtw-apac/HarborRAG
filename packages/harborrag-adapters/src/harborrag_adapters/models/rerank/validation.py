from __future__ import annotations

from harborrag_adapters.models.runtime.config import RoutingEngine
from harborrag_adapters.models.runtime.provider_validation import (
    validate_extension_parameters,
    validate_provider_deployment,
    validate_request_headers,
)
from harborrag_core.models.errors import (
    HarborRerankCapabilityError,
    HarborRerankConfigurationError,
    HarborRerankInvalidRequestError,
)
from harborrag_core.models.rerank import HarborRerankRequest

from .configs import (
    HarborRerankClientConfig,
    HarborRerankModelConfig,
    HarborRerankProviderConfig,
)
from .registry import RerankProviderRegistry

_RERANK_TYPED_EXTENSION_FIELDS = frozenset(
    {
        "documents",
        "instruction",
        "max_chunks_per_doc",
        "max_tokens_per_doc",
        "model",
        "query",
        "rank_fields",
        "return_documents",
        "timeout",
        "top_n",
    }
)


def validate_rerank_configuration(
    config: HarborRerankClientConfig, registry: RerankProviderRegistry | None = None
) -> None:
    """Validate provider policy, credentials, endpoints, and enabled rerank routes."""

    if config.routing.engine is RoutingEngine.LITELLM_ROUTER:
        raise HarborRerankConfigurationError(
            "LiteLLM Router does not expose a reliable sync rerank path; use routing.engine=harbor"
        )
    active_registry = registry or RerankProviderRegistry.default()
    for logical_name, logical in config.models.items():
        default_deployment(logical_name, logical)
        for deployment in logical.deployments:
            validate_provider_deployment(
                deployment,
                logical_model=logical_name,
                metadata=active_registry.get(deployment.provider),
                policy=config.security,
                error_type=HarborRerankConfigurationError,
            )


def default_deployment(
    logical_name: str,
    logical: HarborRerankModelConfig,
) -> HarborRerankProviderConfig:
    """Return the deterministic first enabled deployment for request preparation."""

    enabled = [deployment for deployment in logical.deployments if deployment.enabled]
    if not enabled:
        raise HarborRerankConfigurationError(
            "reranking logical model requires at least one enabled deployment",
            logical_model=logical_name,
        )
    return min(enabled, key=lambda deployment: (deployment.order, deployment.name))


def validate_rerank_request(
    request: HarborRerankRequest,
    config: HarborRerankClientConfig,
    deployment: HarborRerankProviderConfig,
) -> None:
    """Validate request limits, provider capabilities, and extension security."""

    _validate_request_limits(request, config, deployment)
    _validate_request_extensions(request, config)
    _validate_request_capabilities(request, deployment)


def _validate_request_limits(
    request: HarborRerankRequest,
    config: HarborRerankClientConfig,
    deployment: HarborRerankProviderConfig,
) -> None:
    if len(request.query) > config.max_query_characters:
        raise _invalid(request, "rerank query exceeds max_query_characters")
    limits = [config.max_documents_per_request]
    if deployment.max_documents is not None:
        limits.append(deployment.max_documents)
    if deployment.capabilities.max_documents is not None:
        limits.append(deployment.capabilities.max_documents)
    if len(request.documents) > min(limits):
        raise _invalid(request, f"rerank request exceeds document limit={min(limits)}")
    for document in request.documents:
        size = (
            len(document.content)
            if isinstance(document.content, str)
            else len(str(document.content))
        )
        if size > config.max_document_characters:
            raise _invalid(request, "rerank document exceeds max_document_characters")
    if len(request.extra_params) > config.security.max_extra_params:
        raise _invalid(request, "too many request extra_params")


def _validate_request_extensions(
    request: HarborRerankRequest,
    config: HarborRerankClientConfig,
) -> None:
    try:
        validate_extension_parameters(
            request.extra_params,
            allowed=config.security.allowed_extra_litellm_params,
            reserved=_RERANK_TYPED_EXTENSION_FIELDS,
        )
        validate_request_headers(
            request.custom_headers,
            allow_auth_headers=config.security.allow_request_auth_headers,
        )
    except ValueError as exc:
        raise _invalid(request, str(exc), exc) from exc


def _validate_request_capabilities(
    request: HarborRerankRequest,
    deployment: HarborRerankProviderConfig,
) -> None:
    capabilities = deployment.capabilities
    checks = (
        (
            any(isinstance(document.content, dict) for document in request.documents)
            and not capabilities.structured_documents,
            "structured rerank documents",
        ),
        (bool(request.rank_fields) and not capabilities.rank_fields, "rank_fields"),
        (
            request.return_documents is True and not capabilities.return_documents,
            "returned documents",
        ),
        (
            request.max_chunks_per_doc is not None and not capabilities.max_chunks_per_doc,
            "max_chunks_per_doc",
        ),
        (
            request.max_tokens_per_doc is not None and not capabilities.max_tokens_per_doc,
            "max_tokens_per_doc",
        ),
        (
            request.instruction is not None and not capabilities.instruction,
            "instructions",
        ),
    )
    for unsupported, feature in checks:
        if unsupported:
            raise _capability(request, deployment, feature)


def _invalid(
    request: HarborRerankRequest,
    message: str,
    original: Exception | None = None,
) -> HarborRerankInvalidRequestError:
    return HarborRerankInvalidRequestError(
        message,
        operation="rerank",
        logical_model=request.logical_model,
        request_id=request.metadata.request_id,
        retryable=False,
        original_exception=original,
    )


def _capability(
    request: HarborRerankRequest,
    deployment: HarborRerankProviderConfig,
    feature: str,
) -> HarborRerankCapabilityError:
    return HarborRerankCapabilityError(
        f"deployment does not support {feature}",
        operation="rerank",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
    )
