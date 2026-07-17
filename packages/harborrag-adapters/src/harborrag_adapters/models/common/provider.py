from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, Self, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .security import HeaderValue, SecretReference, SecretValue, sanitize_configuration
from .transport import protect_sensitive_headers


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Describe provider identity, requirements, and conservative capabilities."""

    name: StrEnum
    litellm_provider: str | None
    required_fields: frozenset[str] = frozenset()
    requires_api_key: bool = False
    supports_ambient_credentials: bool = False
    requires_custom_base_url: bool = False
    explicit_credential_sets: tuple[frozenset[str], ...] = ()


class ProviderDescriptorProtocol[ProviderKey](Protocol):
    """Expose a registry key from a provider metadata object."""

    @property
    def name(self) -> ProviderKey:
        """Return the provider registry key."""

        ...


class ImmutableProviderRegistry[ProviderKey, Descriptor]:
    """Store provider metadata immutably and reject duplicate registrations."""

    def __init__(
        self, descriptors: Mapping[ProviderKey, Descriptor] | Iterable[Descriptor]
    ) -> None:
        """Copy metadata into an immutable lookup."""

        items: dict[ProviderKey, Descriptor]
        if isinstance(descriptors, Mapping):
            items = dict(descriptors)
        else:
            items = {}
            for descriptor in descriptors:
                key = cast(ProviderDescriptorProtocol[ProviderKey], descriptor).name
                if key in items:
                    raise ValueError(f"duplicate provider registration: {key}")
                items[key] = descriptor
        self._descriptors: Mapping[ProviderKey, Descriptor] = MappingProxyType(items)

    def get(self, provider: ProviderKey) -> Descriptor:
        """Return one provider descriptor or report all supported identifiers."""

        try:
            return self._descriptors[provider]
        except KeyError as exc:
            supported = ", ".join(sorted(str(key) for key in self._descriptors)) or "<none>"
            raise KeyError(
                f"unknown provider {provider!r}; supported providers: {supported}"
            ) from exc

    def all(self) -> Mapping[ProviderKey, Descriptor]:
        """Return the immutable provider mapping."""

        return self._descriptors


class ProviderDeploymentConfig(BaseModel):
    """Define provider model identity, credentials, transport, and routing limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: SecretValue | None = None
    api_base: str | None = None
    api_version: str | None = None
    deployment_name: str | None = None
    custom_llm_provider: str | None = None

    aws_region_name: str | None = None
    aws_access_key_id: SecretValue | None = None
    aws_secret_access_key: SecretValue | None = None
    aws_session_token: SecretValue | None = None
    aws_role_name: str | None = None
    aws_role_session_name: str | None = None

    vertex_project: str | None = None
    vertex_location: str | None = None
    vertex_credentials: SecretValue | None = None

    headers: dict[str, HeaderValue] = Field(default_factory=dict)
    weight: float = Field(default=1.0, gt=0)
    order: int = Field(default=0, ge=0)
    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)
    max_parallel_requests: int | None = Field(default=None, gt=0)
    enabled: bool = True
    allow_ambient_credentials: bool = False
    extra_litellm_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str | None) -> str | None:
        """Require an absolute HTTP(S) URL when a custom API base is supplied."""

        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("api_base must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("api_base must not contain user information")
        if parsed.query or parsed.fragment:
            raise ValueError("api_base must not contain query or fragment data")
        return value.rstrip("/")

    @field_validator("headers", mode="before")
    @classmethod
    def protect_headers(cls, value: Any) -> Any:
        """Wrap authentication-like header values before repr rendering."""

        return protect_sensitive_headers(value)

    @model_validator(mode="after")
    def validate_credential_combinations(self) -> Self:
        """Reject partial AWS credentials and malformed custom headers."""

        static_aws_credentials = (self.aws_access_key_id, self.aws_secret_access_key)
        if any(value is not None for value in static_aws_credentials) and not all(
            value is not None for value in static_aws_credentials
        ):
            raise ValueError(
                "aws_access_key_id and aws_secret_access_key must be configured together"
            )
        invalid_headers = [
            name for name in self.headers if not name.strip() or "\n" in name or "\r" in name
        ]
        if invalid_headers:
            raise ValueError("header names must be non-empty and cannot contain newlines")
        invalid_values = [
            name for name, value in self.headers.items() if _contains_header_newline(value)
        ]
        if invalid_values:
            raise ValueError("header values cannot contain newlines")
        return self

    def validate_provider_metadata(self, metadata: ProviderMetadata) -> Self:
        """Enforce one family registry's provider requirements."""

        missing = [field for field in metadata.required_fields if not getattr(self, field)]
        if missing:
            fields = ", ".join(sorted(missing))
            raise ValueError(f"{metadata.name.value} deployment {self.name!r} requires: {fields}")
        if self.allow_ambient_credentials and not metadata.supports_ambient_credentials:
            raise ValueError(
                f"{metadata.name.value} deployment {self.name!r} does not support "
                "ambient credentials"
            )
        if (
            metadata.supports_ambient_credentials
            and not self.allow_ambient_credentials
            and not self.has_explicit_credentials_for(metadata)
        ):
            raise ValueError(
                f"{metadata.name.value} deployment {self.name!r} requires explicit "
                "credentials or allow_ambient_credentials=true"
            )
        if metadata.requires_api_key and self.api_key is None:
            raise ValueError(
                f"{metadata.name.value} deployment {self.name!r} requires api_key credentials"
            )
        return self

    def has_explicit_credentials_for(self, metadata: ProviderMetadata) -> bool:
        """Return whether one provider's complete explicit credential set is configured."""

        credential_sets = list(metadata.explicit_credential_sets)
        if metadata.requires_api_key:
            credential_sets.append(frozenset({"api_key"}))
        return any(
            all(getattr(self, field) is not None for field in fields) for fields in credential_sets
        )

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        """Redact sensitive provider extension values in nested configuration reprs."""

        for name, value in super().__repr_args__():
            if name == "extra_litellm_params":
                wrapper = ProviderExtensionParameters(values=value)
                yield name, sanitize_configuration(wrapper)["values"]
            else:
                yield name, value


class ProviderExtensionParameters(BaseModel):
    """Provide an internal sanitization boundary for LiteLLM extension parameters."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    values: dict[str, Any]


def _contains_header_newline(value: HeaderValue) -> bool:
    """Return whether a configured header value can inject another header line."""

    if isinstance(value, SecretReference):
        raw = value.uri
    elif hasattr(value, "get_secret_value"):
        raw = value.get_secret_value()
    else:
        raw = str(value)
    return "\r" in raw or "\n" in raw
