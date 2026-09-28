"""Narrow application-service dependency for provider screen routes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.domain.provider import ProviderFamily


class ProvidersService(Protocol):
    async def list_providers(self, *, tenant_ids: frozenset[str] | None) -> AppResponse: ...

    async def get_provider(
        self, provider_id: str, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse: ...

    async def create_provider(  # noqa: PLR0913 - mirrors the workflow_control facade
        self,
        *,
        tenant_id: str,
        name: str,
        family: ProviderFamily,
        config: Mapping[str, object],
        api_key: str | None,
        actor: str,
    ) -> AppResponse: ...

    async def update_provider(
        self,
        provider_id: str,
        *,
        updates: dict[str, object],
        actor: str,
        tenant_ids: frozenset[str] | None,
    ) -> AppResponse: ...

    async def delete_provider(
        self, provider_id: str, *, actor: str, tenant_ids: frozenset[str] | None
    ) -> AppResponse: ...

    async def test_provider_connection(
        self, provider_id: str, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse: ...

    async def list_routing_rules(self) -> AppResponse: ...

    async def replace_routing_rules(
        self, rules: Sequence[Mapping[str, object]], *, actor: str
    ) -> AppResponse: ...

    async def get_provider_cost(self, *, tenant_ids: frozenset[str] | None) -> AppResponse: ...


def providers_service(request: Request) -> ProvidersService:
    return cast(ProvidersService, request.app.state.app_service)


ProvidersServiceDependency = Annotated[ProvidersService, Depends(providers_service)]
