from __future__ import annotations

from typing import Any, Self

from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import HarborEmbedRequest, HarborEmbedResponse, RawEmbeddingInput
from harborrag_core.models.protocols import (
    AsyncHarborEmbedClientProtocol,
    HarborEmbedClientProtocol,
)

from harborrag_adapters.models.common.cache import ModelResponseCache
from harborrag_adapters.models.common.config import RoutingEngine
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.common.litellm_router import build_litellm_router
from harborrag_adapters.models.common.telemetry import TelemetryDispatcher
from .configs import HarborEmbedClientConfig
from .execution import EmbedExecution
from .invocation import (
    EmbeddingInvocation,
    LiteLLMEmbeddingInvocation,
    LiteLLMEmbeddingRouterInvocation,
)
from .parameters import prepare_embed_request
from .validation import validate_embed_configuration


class HarborEmbedClient(HarborEmbedClientProtocol, AsyncHarborEmbedClientProtocol):
    """Provide independent synchronous and asynchronous LiteLLM embeddings."""

    def __init__(
        self,
        config: HarborEmbedClientConfig,
        *,
        invocation: EmbeddingInvocation | None = None,
        cache: ModelResponseCache | None = None,
        resource_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        telemetry: TelemetryDispatcher | None = None,
        telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
    ) -> None:
        """Validate configuration and store the injected provider boundary."""

        validate_embed_configuration(config)
        self.config = config
        self._invocation = invocation or self._default_invocation(config)
        self._telemetry = telemetry or TelemetryDispatcher((), config=config.observability)
        self._owns_telemetry = telemetry is None or telemetry_ownership is ResourceOwnership.OWNED
        self._execution = EmbedExecution(
            config,
            self._invocation,
            cache=cache,
            telemetry=self._telemetry,
        )
        self._resource_ownership = resource_ownership
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: HarborEmbedClientConfig,
        *,
        invocation: EmbeddingInvocation | None = None,
        cache: ModelResponseCache | None = None,
        resource_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        telemetry: TelemetryDispatcher | None = None,
        telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
    ) -> Self:
        """Construct a client from a validated Python configuration object."""

        return cls(
            config,
            invocation=invocation,
            cache=cache,
            resource_ownership=resource_ownership,
            telemetry=telemetry,
            telemetry_ownership=telemetry_ownership,
        )

    def __enter__(self) -> Self:
        """Enter the synchronous lifecycle context."""

        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close owned invocation resources on synchronous context exit."""

        self.close()

    async def __aenter__(self) -> Self:
        """Enter the asynchronous lifecycle context."""

        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close owned invocation resources on asynchronous context exit."""

        await self.aclose()

    def embed(
        self,
        inputs: RawEmbeddingInput | None = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        """Embed one input or batch synchronously in stable input order."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical, _, prepared = prepare_embed_request(
            self.config,
            inputs,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return self._execution.embed(logical, prepared, model_alias=model_alias)

    async def aembed(
        self,
        inputs: RawEmbeddingInput | None = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        """Embed one input or batch asynchronously in stable input order."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical, _, prepared = prepare_embed_request(
            self.config,
            inputs,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return await self._execution.aembed(logical, prepared, model_alias=model_alias)

    def capabilities(self, model: str | None = None) -> dict[str, HarborEmbedCapabilities]:
        """Return explicitly configured capabilities for one logical model."""

        logical_name, _ = self.config.model_for(model)
        return {
            deployment.name: deployment.capabilities
            for deployment in self.config.models[logical_name].deployments
        }

    def close(self) -> None:
        """Close an owned invocation exactly once."""

        if self._closed:
            return
        self._closed = True
        if self._resource_ownership is ResourceOwnership.OWNED:
            self._invocation.close()
        if self._execution.owns_cache:
            self._execution.cache.backend.close()
        if self._owns_telemetry:
            self._telemetry.close()

    async def aclose(self) -> None:
        """Asynchronously close an owned invocation exactly once."""

        if self._closed:
            return
        self._closed = True
        if self._resource_ownership is ResourceOwnership.OWNED:
            await self._invocation.aclose()
        if self._execution.owns_cache:
            await self._execution.cache.backend.aclose()
        if self._owns_telemetry:
            await self._telemetry.aclose()

    @staticmethod
    def _default_invocation(config: HarborEmbedClientConfig) -> EmbeddingInvocation:
        if config.routing.engine is not RoutingEngine.LITELLM_ROUTER:
            return LiteLLMEmbeddingInvocation()
        from .registry import EmbedProviderRegistry

        registry = EmbedProviderRegistry.default()
        router = build_litellm_router(
            config,
            config.models,
            provider_resolver=lambda deployment: registry.get(deployment.provider).litellm_provider,
        )
        return LiteLLMEmbeddingRouterInvocation(router)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("HarborEmbedClient is closed")


AsyncHarborEmbedClient = HarborEmbedClient

__all__ = ["AsyncHarborEmbedClient", "HarborEmbedClient"]
