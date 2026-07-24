from __future__ import annotations

from enum import StrEnum
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from harborrag_adapters.models.runtime.config import RoutingEngine
from harborrag_adapters.models.runtime.security import (
    HeaderValue,
    SecretReference,
    SecretValue,
)
from harborrag_adapters.models.runtime.transport import protect_sensitive_headers


class ChatBackendType(StrEnum):
    """Identify the concrete LiteLLM transport used by the chat client."""

    AUTO = "auto"
    DIRECT_SDK = "direct_sdk"
    LITELLM_ROUTER = "litellm_router"
    LITELLM_PROXY = "litellm_proxy"


class ProxyAuthMode(StrEnum):
    """Select how Harbor authenticates with a LiteLLM Proxy."""

    BEARER = "bearer"
    X_LITELLM_API_KEY = "x_litellm_api_key"
    CUSTOM_HEADER = "custom_header"


class ProxyMetadataConfig(BaseModel):
    """Configure safe Harbor metadata propagation to LiteLLM Proxy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    propagate_headers: bool = True
    request_id_header: str = "x-harbor-request-id"
    trace_id_header: str = "x-harbor-trace-id"
    tenant_id_header: str | None = None
    user_id_header: str | None = None


class LiteLLMProxyConfig(BaseModel):
    """Configure one authenticated LiteLLM Proxy endpoint shared by chat models."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base: str
    api_key: SecretValue
    headers: dict[str, HeaderValue] = Field(default_factory=dict)
    model_prefix: str = "litellm_proxy/"
    auth_mode: ProxyAuthMode = ProxyAuthMode.BEARER
    auth_header_name: str | None = None
    metadata: ProxyMetadataConfig = Field(default_factory=ProxyMetadataConfig)

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        """Require an absolute endpoint without embedded credentials or query secrets."""

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("proxy api_base must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("proxy api_base must not contain user information")
        if parsed.query or parsed.fragment:
            raise ValueError("proxy api_base must not contain query or fragment data")
        return value.rstrip("/")

    @field_validator("headers", mode="before")
    @classmethod
    def protect_headers(cls, value: Any) -> Any:
        """Redact authentication-like headers and reject newline injection."""

        protected = protect_sensitive_headers(value)
        if isinstance(protected, dict) and any(
            _header_contains_newline(item) for item in protected.values()
        ):
            raise ValueError("proxy header values cannot contain newlines")
        return protected

    @model_validator(mode="after")
    def validate_authentication(self) -> Self:
        """Require a safe custom header name only for custom-header authentication."""

        if self.auth_mode is ProxyAuthMode.CUSTOM_HEADER and not self.auth_header_name:
            raise ValueError("auth_header_name is required for custom-header proxy auth")
        if self.auth_mode is not ProxyAuthMode.CUSTOM_HEADER and self.auth_header_name is not None:
            raise ValueError("auth_header_name is only valid for custom-header proxy auth")
        if self.auth_header_name and (
            not self.auth_header_name.strip()
            or "\n" in self.auth_header_name
            or "\r" in self.auth_header_name
        ):
            raise ValueError("proxy auth header name is invalid")
        return self

    @field_validator("model_prefix")
    @classmethod
    def validate_model_prefix(cls, value: str) -> str:
        """Require a non-empty prefix ending in one provider separator."""

        if not value or not value.endswith("/"):
            raise ValueError("model_prefix must be non-empty and end with '/'")
        return value


class ChatBackendConfig(BaseModel):
    """Select direct SDK, in-process Router, or remote Proxy chat transport."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ChatBackendType = ChatBackendType.AUTO
    proxy: LiteLLMProxyConfig | None = None

    @model_validator(mode="after")
    def validate_proxy_settings(self) -> Self:
        """Require proxy credentials only for the remote Proxy backend."""

        if self.type is ChatBackendType.LITELLM_PROXY and self.proxy is None:
            raise ValueError("backend.proxy is required for the LiteLLM Proxy backend")
        if self.type is not ChatBackendType.LITELLM_PROXY and self.proxy is not None:
            raise ValueError("backend.proxy is only valid for the LiteLLM Proxy backend")
        return self

    def resolved_type(self, routing_engine: RoutingEngine) -> ChatBackendType:
        """Resolve backward-compatible automatic selection from the routing engine."""

        if self.type is not ChatBackendType.AUTO:
            return self.type
        if routing_engine is RoutingEngine.LITELLM_ROUTER:
            return ChatBackendType.LITELLM_ROUTER
        return ChatBackendType.DIRECT_SDK


def _header_contains_newline(value: HeaderValue) -> bool:
    """Return whether one resolved or literal header value contains CR/LF characters."""

    if isinstance(value, SecretReference):
        return False
    get_secret_value = getattr(value, "get_secret_value", None)
    plain = get_secret_value() if callable(get_secret_value) else str(value)
    return "\n" in plain or "\r" in plain
