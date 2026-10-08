from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Any, Protocol

from .provider import ProviderDeploymentConfig, ProviderMetadata
from .security import HeaderValue, SecretReference
from .transport import validate_base_url

PROVIDER_RESERVED_EXTRA_PARAMS = frozenset(
    {
        "api_base",
        "api_key",
        "api_version",
        "aws_access_key_id",
        "aws_region_name",
        "aws_role_name",
        "aws_role_session_name",
        "aws_secret_access_key",
        "aws_session_token",
        "custom_llm_provider",
        "deployment_name",
        "extra_headers",
        "headers",
        "max_parallel_requests",
        "model",
        "rpm",
        "tpm",
        "vertex_credentials",
        "vertex_location",
        "vertex_project",
    }
)

ENDPOINT_EXTENSION_PARAMS = frozenset(
    {
        "aws_bedrock_runtime_endpoint",
        "aws_sts_endpoint",
        "azure_endpoint",
        "base_url",
        "endpoint",
    }
)
"""LiteLLM extension keys known to redirect provider traffic."""

_ENDPOINT_EXTENSION_SUFFIXES = ("_endpoint", "_url", "_base")
_URL_PREFIXES = ("http://", "https://")

_AUTH_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key"})


class ProviderSecurityPolicy(Protocol):
    """Expose read-only provider restrictions used by every model family."""

    @property
    def allowed_providers(self) -> Collection[StrEnum] | None:
        """Return the providers permitted by application policy."""
        ...

    @property
    def allowed_extra_litellm_params(self) -> Collection[str]:
        """Return provider extension names that may cross the SDK boundary."""
        ...

    @property
    def allow_custom_providers(self) -> bool:
        """Return whether arbitrary LiteLLM providers are enabled."""
        ...

    @property
    def allowed_base_url_hosts(self) -> frozenset[str] | None:
        """Return the optional allowlist for custom provider endpoints."""
        ...

    @property
    def require_https_for_remote_endpoints(self) -> bool:
        """Return whether non-loopback custom endpoints must use HTTPS."""
        ...


class ConfigurationErrorFactory(Protocol):
    """Create a family-specific configuration error with diagnostic context."""

    def __call__(
        self,
        message: str,
        *,
        logical_model: str | None = None,
        deployment: str | None = None,
        original_exception: Exception | None = None,
    ) -> Exception:
        """Return one typed configuration exception."""

        ...


def validate_provider_deployment(
    deployment: ProviderDeploymentConfig,
    *,
    logical_model: str,
    metadata: ProviderMetadata,
    policy: ProviderSecurityPolicy,
    error_type: ConfigurationErrorFactory,
    reserved_extra_params: Collection[str] = PROVIDER_RESERVED_EXTRA_PARAMS,
) -> None:
    """Apply common provider, endpoint, credential, and extension security rules."""

    provider = getattr(deployment, "provider", metadata.name)
    allowed = policy.allowed_providers
    if allowed is not None and provider not in allowed:
        raise error_type(
            f"provider {str(provider)!r} is not allowed",
            logical_model=logical_model,
            deployment=deployment.name,
        )
    provider_value = getattr(provider, "value", str(provider))
    if provider_value == "custom" and not policy.allow_custom_providers:
        raise error_type(
            "custom providers are disabled by security policy",
            logical_model=logical_model,
            deployment=deployment.name,
        )
    try:
        deployment.validate_provider_metadata(metadata)
        validate_base_url(
            deployment.api_base,
            allowed_hosts=policy.allowed_base_url_hosts,
            require_https=policy.require_https_for_remote_endpoints,
        )
        validate_extension_parameters(
            deployment.extra_litellm_params,
            allowed=policy.allowed_extra_litellm_params,
            reserved=reserved_extra_params,
        )
        validate_endpoint_extension_parameters(
            deployment.extra_litellm_params,
            allowed_hosts=policy.allowed_base_url_hosts,
            require_https=policy.require_https_for_remote_endpoints,
        )
    except ValueError as exc:
        raise error_type(
            str(exc),
            logical_model=logical_model,
            deployment=deployment.name,
            original_exception=exc,
        ) from exc


def validate_extension_parameters(
    values: Mapping[str, Any],
    *,
    allowed: Collection[str],
    reserved: Collection[str] = (),
) -> None:
    """Reject unapproved or typed parameters supplied through extension dictionaries."""

    unknown = set(values).difference(allowed)
    if unknown:
        raise ValueError("unapproved LiteLLM parameters: " + ", ".join(sorted(unknown)))
    conflicts = set(values).intersection(reserved)
    if conflicts:
        raise ValueError(
            "typed parameters cannot be supplied through LiteLLM extensions: "
            + ", ".join(sorted(conflicts))
        )


def is_endpoint_extension_parameter(name: str, value: object) -> bool:
    """Return whether an extension parameter can redirect provider traffic.

    Known names and ``_endpoint``/``_url``/``_base`` suffixes match regardless of
    value; any string value that is itself an HTTP(S) URL matches regardless of
    name, so allowlisting a new LiteLLM key cannot bypass base-URL policy.
    """

    lowered = name.lower()
    if lowered in ENDPOINT_EXTENSION_PARAMS or lowered.endswith(_ENDPOINT_EXTENSION_SUFFIXES):
        return True
    return isinstance(value, str) and value.strip().lower().startswith(_URL_PREFIXES)


def validate_endpoint_extension_parameters(
    values: Mapping[str, Any],
    *,
    allowed_hosts: frozenset[str] | None,
    require_https: bool,
) -> None:
    """Apply deployment base-URL policy to every endpoint-bearing extension parameter."""

    for name, value in values.items():
        if not is_endpoint_extension_parameter(name, value):
            continue
        if not isinstance(value, str):
            raise ValueError(f"LiteLLM endpoint parameter {name!r} must be a URL string")
        try:
            validate_base_url(value, allowed_hosts=allowed_hosts, require_https=require_https)
        except ValueError as exc:
            raise ValueError(f"LiteLLM endpoint parameter {name!r}: {exc}") from exc


def reject_endpoint_extension_parameters(values: Mapping[str, Any]) -> None:
    """Forbid per-request extension parameters that would redirect provider traffic."""

    overrides = sorted(
        name for name, value in values.items() if is_endpoint_extension_parameter(name, value)
    )
    if overrides:
        raise ValueError(
            "request-level LiteLLM endpoint overrides are not allowed: " + ", ".join(overrides)
        )


def validate_request_headers(
    headers: Mapping[str, HeaderValue], *, allow_auth_headers: bool
) -> None:
    """Reject header injection and unauthorized request-level credentials."""

    invalid_names = [name for name in headers if not name.strip() or "\r" in name or "\n" in name]
    if invalid_names:
        raise ValueError("request header names must be non-empty and cannot contain newlines")
    invalid_values = [name for name, value in headers.items() if _header_newline(value)]
    if invalid_values:
        raise ValueError("request header values cannot contain newlines")
    authentication = {name for name in headers if name.lower() in _AUTH_HEADERS}
    if authentication and not allow_auth_headers:
        raise ValueError(
            "request-level authentication headers are disabled: "
            + ", ".join(sorted(authentication))
        )


def _header_newline(value: HeaderValue) -> bool:
    if isinstance(value, SecretReference):
        raw = value.uri
    elif hasattr(value, "get_secret_value"):
        raw = value.get_secret_value()
    else:
        raw = str(value)
    return "\r" in raw or "\n" in raw
