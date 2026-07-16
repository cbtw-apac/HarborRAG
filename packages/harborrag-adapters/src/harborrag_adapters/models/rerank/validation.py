from __future__ import annotations

from harborrag_core.models.errors import (
    HarborRerankCapabilityError,
    HarborRerankConfigurationError,
    HarborRerankInvalidRequestError,
)
from harborrag_core.models.rerank import HarborRerankRequest

from harborrag_core.models.common.config import RoutingEngine
from harborrag_core.models.common.transport import validate_base_url
from .configs import (
    HarborRerankClientConfig,
    HarborRerankModelConfig,
    HarborRerankProviderConfig,
)
from .registry import HarborRerankProvider

_AUTH_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key"})


def validate_rerank_configuration(config: HarborRerankClientConfig) -> None:
    """Validate security and require an enabled route for every model."""

    if config.routing.engine is RoutingEngine.LITELLM_ROUTER:
        raise HarborRerankConfigurationError(
            "LiteLLM Router does not expose a reliable sync rerank path; use routing.engine=harbor"
        )
    try:
        for logical_name, logical in config.models.items():
            default_deployment(logical_name, logical)
            for deployment in logical.deployments:
                validate_base_url(
                    deployment.api_base,
                    allowed_hosts=config.security.allowed_base_url_hosts,
                    require_https=config.security.require_https_for_remote_endpoints,
                )
                if (
                    deployment.provider is HarborRerankProvider.CUSTOM
                    and not config.security.allow_custom_providers
                ):
                    raise ValueError("custom rerank providers are disabled")
                unknown = set(deployment.extra_litellm_params).difference(
                    config.security.allowed_extra_litellm_params
                )
                if unknown:
                    raise ValueError(
                        f"deployment {deployment.name!r} contains disallowed LiteLLM "
                        f"parameters: {', '.join(sorted(unknown))}"
                    )
    except ValueError as exc:
        raise HarborRerankConfigurationError(str(exc), original_exception=exc) from exc


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
    unknown = set(request.extra_params).difference(config.security.allowed_extra_litellm_params)
    if unknown:
        raise _invalid(
            request,
            "request contains disallowed LiteLLM parameters: " + ", ".join(sorted(unknown)),
        )
    auth_headers = {name.lower() for name in request.custom_headers}.intersection(_AUTH_HEADERS)
    if auth_headers and not config.security.allow_request_auth_headers:
        raise _invalid(request, "request-level authentication headers are disabled")

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
        (request.instruction is not None and not capabilities.instruction, "instructions"),
    )
    for unsupported, feature in checks:
        if unsupported:
            raise _capability(request, deployment, feature)


def _invalid(request: HarborRerankRequest, message: str) -> HarborRerankInvalidRequestError:
    return HarborRerankInvalidRequestError(
        message,
        operation="rerank",
        logical_model=request.logical_model,
        request_id=request.metadata.request_id,
        retryable=False,
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
