from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Self

from harborrag_core.models.capabilities import HarborRerankCapabilities
from harborrag_core.models.protocols import (
    AsyncHarborRerankingClientProtocol,
    HarborRerankingClientProtocol,
)
from harborrag_core.models.rerank import (
    HarborRerankRequest,
    HarborRerankResponse,
    RawRerankDocument,
)

from harborrag_adapters.models.common.cache import ModelResponseCache
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.common.telemetry import TelemetryDispatcher
from .configs import HarborRerankClientConfig
from .execution import RerankExecution
from .invocation import LiteLLMRerankInvocation, RerankInvocation
from .parameters import prepare_rerank_request
from .validation import validate_rerank_configuration


class HarborRerankingClient(
    HarborRerankingClientProtocol,
    AsyncHarborRerankingClientProtocol,
):
    """Provide independent synchronous and asynchronous provider-neutral reranking."""

    def __init__(
        self,
        config: HarborRerankClientConfig,
        *,
        invocation: RerankInvocation | None = None,
        cache: ModelResponseCache | None = None,
        resource_ownership: ResourceOwnership = ResourceOwnership.OWNED,
        telemetry: TelemetryDispatcher | None = None,
        telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
    ) -> None:
        """Validate configuration and store the injected reranking boundary."""

        validate_rerank_configuration(config)
        self.config = config
        self._invocation = invocation or LiteLLMRerankInvocation()
        self._telemetry = telemetry or TelemetryDispatcher((), config=config.observability)
        self._owns_telemetry = telemetry is None or telemetry_ownership is ResourceOwnership.OWNED
        self._execution = RerankExecution(
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
        config: HarborRerankClientConfig,
        *,
        invocation: RerankInvocation | None = None,
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

    def rerank(
        self,
        query: str | None = None,
        documents: Sequence[RawRerankDocument] | None = None,
        *,
        request: HarborRerankRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborRerankResponse:
        """Rerank a complete candidate set synchronously."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical, _, prepared = prepare_rerank_request(
            self.config,
            query,
            documents,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return self._execution.rerank(logical, prepared, model_alias=model_alias)

    async def arerank(
        self,
        query: str | None = None,
        documents: Sequence[RawRerankDocument] | None = None,
        *,
        request: HarborRerankRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborRerankResponse:
        """Rerank a complete candidate set asynchronously."""

        self._ensure_open()
        model_alias = model or (request.logical_model if request is not None else None)
        model_alias = model_alias or self.config.default_model
        logical, _, prepared = prepare_rerank_request(
            self.config,
            query,
            documents,
            request=request,
            model=model,
            request_kwargs=kwargs,
        )
        return await self._execution.arerank(logical, prepared, model_alias=model_alias)

    def capabilities(self, model: str | None = None) -> dict[str, HarborRerankCapabilities]:
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

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("HarborRerankingClient is closed")


AsyncHarborRerankingClient = HarborRerankingClient

__all__ = ["AsyncHarborRerankingClient", "HarborRerankingClient"]
