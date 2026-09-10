"""Runtime chat orchestration and client lifecycle.

Two clients can answer a turn. The process-wide one is built from
``config/models.yaml`` and is what every deployment has always used. A tenant
that stored its own models gets its own client instead, cached and validated
against the catalog fingerprint (see ``tenant_clients``). Which one answers is
decided per request from ``request.metadata.tenant_id``, and any failure to
resolve a tenant's own client falls back to the process-wide one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from harborrag_core.ports.model_clients import AsyncHarborChatClientProtocol
from harborrag_runtime.config.settings import RuntimeSettings

from .prompts import ChatPrompt, PromptCatalog
from .tenant_clients import TenantChatClients, TenantChatResolution, TenantClientBuilder
from .tenant_models import TenantModelSources

if TYPE_CHECKING:
    from harborrag_adapters.models.chat import HarborChatClientConfig

type ChatClientBuilder = Callable[[RuntimeSettings], AsyncHarborChatClientProtocol]
type SharedConfigLoader = Callable[[RuntimeSettings], HarborChatClientConfig]

MODEL_FIELD = "model"


def _load_shared_config(settings: RuntimeSettings) -> HarborChatClientConfig:
    # Imported here, not at module scope, so importing this package never pulls
    # in the model provider runtime -- the CLI's submission path relies on it.
    from harborrag_adapters.models.chat import HarborChatClientConfig

    return HarborChatClientConfig.from_file(settings.model_config_path)


class RuntimeChatService:
    """Apply server-owned prompts and execute chat on the right client."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        client_builder: ChatClientBuilder | None = None,
        prompts: PromptCatalog | None = None,
        shared_config_loader: SharedConfigLoader = _load_shared_config,
    ) -> None:
        self._settings = settings
        self._client_builder = client_builder
        self._prompts = prompts or PromptCatalog.packaged()
        self._shared_config_loader = shared_config_loader
        self._client: AsyncHarborChatClientProtocol | None = None
        self._client_lock = asyncio.Lock()
        self._shared_config: HarborChatClientConfig | None = None
        self._tenants: TenantChatClients | None = None

    def configure_tenant_models(
        self,
        sources: TenantModelSources,
        *,
        builder: TenantClientBuilder | None = None,
    ) -> None:
        """Enable per-tenant catalogs, once, before the first turn.

        Wired at composition rather than taken from configuration because both
        halves are control-plane ports: the SDK is constructed from settings
        alone and must keep working for a deployment that wires neither.
        """

        self._tenants = TenantChatClients(
            self._settings,
            sources,
            builder=builder or self._tenant_client,
        )

    async def validate_model(self, model: str | None, *, tenant_id: str | None) -> None:
        """Reject a model name the tenant that asked for it may not use.

        A tenant with its own catalog is bounded by that catalog; every other
        tenant is bounded by the process-wide one, which is also what a tenant
        whose own configuration failed to build falls back to. ``None`` means
        the caller chose nothing and the resolved default answers, exactly as
        before this existed.
        """

        if model is None:
            return
        resolution = await self._resolution(tenant_id)
        if resolution.catalog is not None:
            if resolution.catalog.allows(model):
                return
            raise HarborValidationError(
                f"{MODEL_FIELD} {model!r} is not configured for this tenant",
                {"field": MODEL_FIELD},
            )
        if self._configured_catalog().resolve_alias(model) is None:
            raise HarborValidationError(
                f"{MODEL_FIELD} {model!r} is not a configured chat model",
                {"field": MODEL_FIELD},
            )

    async def complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> HarborChatResponse:
        prepared = self._apply_prompt(request, prompt)
        client = await self._client_for(request)
        return await client.achat(request=prepared)

    async def stream(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        prepared = self._apply_prompt(request, prompt)
        client = await self._client_for(request)
        async for chunk in client.astream(request=prepared):
            yield chunk

    async def aclose(self) -> None:
        """Swap and close the client under the same lock ``_configured_client`` uses.

        Without the lock, a concurrent ``_configured_client()`` call can miss
        the fast path, see the field already nulled out here, and build a
        second client that this call never closes -- a leak under concurrent
        shutdown. Every cached tenant client is disposed too; each holds its
        own connection pool.
        """
        if self._tenants is not None:
            await self._tenants.aclose()
        async with self._client_lock:
            client, self._client = self._client, None
            if client is not None:
                await client.aclose()

    def _apply_prompt(
        self,
        request: HarborChatRequest,
        prompt: ChatPrompt | None,
    ) -> HarborChatRequest:
        if prompt is None:
            return request
        system_message = HarborChatMessage.system(self._prompts.resolve(prompt))
        return request.model_copy(update={"messages": (system_message, *request.messages)})

    async def _client_for(self, request: HarborChatRequest) -> AsyncHarborChatClientProtocol:
        resolution = await self._resolution(request.metadata.tenant_id)
        if resolution.client is not None:
            return resolution.client
        return await self._configured_client()

    def _tenant_client(self, config: HarborChatClientConfig) -> AsyncHarborChatClientProtocol:
        from .composition import build_chat_client_for

        return build_chat_client_for(config, self._settings)

    async def _resolution(self, tenant_id: str | None) -> TenantChatResolution:
        if self._tenants is None:
            return TenantChatResolution()
        return await self._tenants.resolve(tenant_id)

    def _configured_catalog(self) -> HarborChatClientConfig:
        """The process-wide catalog, loaded once, for bounding model names."""

        if self._shared_config is None:
            self._shared_config = self._shared_config_loader(self._settings)
        return self._shared_config

    async def _configured_client(self) -> AsyncHarborChatClientProtocol:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                builder = self._client_builder
                if builder is None:
                    from .composition import build_chat_client

                    builder = build_chat_client
                self._client = builder(self._settings)
        return self._client
