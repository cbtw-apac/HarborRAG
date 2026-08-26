from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_adapters.models.runtime.security import reveal_secret
from harborrag_adapters.models.runtime.transport import reveal_headers

from ..backend_config import ChatBackendType, LiteLLMProxyConfig, ProxyAuthMode
from .base import BaseLiteLLMChatBackend

type CompletionCallable = Callable[..., Any]
type AsyncCompletionCallable = Callable[..., Awaitable[Any]]

_PROVIDER_CREDENTIAL_FIELDS = frozenset(
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
        "vertex_credentials",
        "vertex_location",
        "vertex_project",
    }
)
_AUTH_HEADER_NAMES = frozenset(
    {
        "api-key",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-litellm-api-key",
    }
)


class LiteLLMProxyBackend(BaseLiteLLMChatBackend):
    """Send chat requests to one authenticated OpenAI-compatible LiteLLM Proxy."""

    def __init__(
        self,
        proxy: LiteLLMProxyConfig,
        *,
        connections: SharedConnectionLifecycle,
        connection_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
        completion: CompletionCallable | None = None,
        acompletion: AsyncCompletionCallable | None = None,
    ) -> None:
        """Store proxy credentials and bind LiteLLM's proxy-compatible entrypoints."""

        if completion is None or acompletion is None:
            import litellm

            completion = completion or litellm.completion
            acompletion = acompletion or litellm.acompletion
        self._proxy = proxy
        super().__init__(
            ChatBackendType.LITELLM_PROXY,
            completion,
            acompletion,
            connections=connections,
            connection_ownership=connection_ownership,
        )

    def prepare_parameters(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Replace provider credentials with the proxy endpoint and virtual key."""

        params = {
            name: value for name, value in kwargs.items() if name not in _PROVIDER_CREDENTIAL_FIELDS
        }
        model = str(params.get("model") or "")
        if not model:
            raise ValueError("proxy backend requires a model alias")
        if not model.startswith(self._proxy.model_prefix):
            params["model"] = f"{self._proxy.model_prefix}{model}"
        params["api_base"] = self._proxy.api_base
        proxy_key = reveal_secret(self._proxy.api_key)
        configured_headers = reveal_headers(self._proxy.headers)
        request_headers = params.pop("extra_headers", None)
        if isinstance(request_headers, dict):
            self._reject_request_auth_headers(request_headers)
            configured_headers.update(request_headers)
        self._propagate_metadata_headers(params, configured_headers)
        if self._proxy.auth_mode is ProxyAuthMode.BEARER:
            params["api_key"] = proxy_key
        else:
            params["api_key"] = "harbor-proxy-placeholder"
            header = (
                "x-litellm-api-key"
                if self._proxy.auth_mode is ProxyAuthMode.X_LITELLM_API_KEY
                else self._proxy.auth_header_name
            )
            if header is None:
                raise ValueError("proxy custom authentication header is not configured")
            self._remove_auth_headers(configured_headers, selected=header)
            configured_headers[header] = proxy_key or ""
        if configured_headers:
            params["extra_headers"] = configured_headers
        return params

    def _reject_request_auth_headers(self, headers: dict[str, Any]) -> None:
        """Keep caller-controlled headers from replacing proxy authentication."""

        configured_name = (self._proxy.auth_header_name or "").lower()
        denied = _AUTH_HEADER_NAMES | ({configured_name} if configured_name else set())
        if any(name.lower() in denied for name in headers):
            raise ValueError("request headers cannot override LiteLLM Proxy authentication")

    @staticmethod
    def _remove_auth_headers(headers: dict[str, str], *, selected: str) -> None:
        """Remove ambiguous configured credentials before installing the selected one."""

        denied = _AUTH_HEADER_NAMES | {selected.lower()}
        for name in tuple(headers):
            if name.lower() in denied:
                del headers[name]

    def _propagate_metadata_headers(self, params: dict[str, Any], headers: dict[str, str]) -> None:
        """Copy selected already-sanitized metadata into explicit Proxy headers."""

        policy = self._proxy.metadata
        if not policy.propagate_headers:
            return
        metadata = params.get("metadata")
        if not isinstance(metadata, dict):
            return
        harbor = metadata.get("harborrag")
        if not isinstance(harbor, dict):
            return
        mappings = (
            (policy.request_id_header, harbor.get("request_id")),
            (policy.trace_id_header, harbor.get("trace_id")),
            (policy.tenant_id_header, harbor.get("tenant_id")),
            (policy.user_id_header, harbor.get("user_id")),
        )
        for name, value in mappings:
            if name and value:
                headers[name] = str(value)
