"""Provider screen endpoints: CRUD, test-connection, routing, and cost.

Secret-shaped fields never round-trip through this layer -- ``api_key`` is
extracted to the secrets port by the service layer
(workflow_control.control_plane.writes) and only its ``secret_ref`` ever
appears in a response. Test-connection resolves the raw secret behind one
sanctioned boundary (``ProviderProbePort``); this route never touches it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses

from .dependencies import ProvidersServiceDependency
from .schemas import (
    ProviderCostResponse,
    ProviderCreateInput,
    ProviderOut,
    ProviderTestResult,
    ProviderUpdateInput,
    RoutingRuleInput,
    RoutingRuleOut,
)

router = APIRouter(
    prefix="/providers",
    tags=["Providers"],
    dependencies=[Depends(require_api_capacity)],
)

CRUD_ERROR_RESPONSES = documented_error_responses({404: "Provider not found"})
TEST_ERROR_RESPONSES = documented_error_responses(
    {
        404: "Provider not found",
        501: "Test-connection is not supported for this provider family",
    }
)
ROUTING_ERROR_RESPONSES = documented_error_responses({404: "Referenced provider not found"})


@router.get("", response_model=list[ProviderOut], responses=CRUD_ERROR_RESPONSES)
async def list_providers(
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> list[ProviderOut]:
    """Providers visible to the caller's tenants; no real secret value ever appears."""
    response = await service.list_providers(tenant_ids=principal.tenant_scope)
    return [ProviderOut.from_domain(provider) for provider in response.data["providers"]]


@router.get("/routing", response_model=list[RoutingRuleOut])
async def get_routing_rules(
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> list[RoutingRuleOut]:
    """Every routing rule currently in effect (workspace-wide, not tenant-scoped)."""
    del principal
    response = await service.list_routing_rules()
    return [RoutingRuleOut.from_domain(rule) for rule in response.data["rules"]]


@router.put("/routing", response_model=list[RoutingRuleOut], responses=ROUTING_ERROR_RESPONSES)
async def replace_routing_rules(
    rules: list[RoutingRuleInput],
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> list[RoutingRuleOut]:
    """Replace the entire routing table with ``rules`` in one atomic write."""
    response = await service.replace_routing_rules(
        [rule.model_dump() for rule in rules], actor=principal.subject
    )
    return [RoutingRuleOut.from_domain(rule) for rule in response.data["rules"]]


@router.get("/cost", response_model=ProviderCostResponse)
async def get_provider_cost(
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ProviderCostResponse:
    """A live spend snapshot per provider, since the last app restart."""
    response = await service.get_provider_cost(tenant_ids=principal.tenant_scope)
    return ProviderCostResponse.model_validate(response.data)


@router.get("/{provider_id}", response_model=ProviderOut, responses=CRUD_ERROR_RESPONSES)
async def get_provider(
    provider_id: str,
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ProviderOut:
    """One provider by id; 404 (enveloped) when missing or outside the caller's tenants."""
    response = await service.get_provider(provider_id, tenant_ids=principal.tenant_scope)
    return ProviderOut.from_domain(response.data["provider"])


@router.post("", status_code=201, response_model=ProviderOut)
async def create_provider(
    payload: ProviderCreateInput,
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> ProviderOut:
    """Create a provider; ``api_key`` (if given) never round-trips in the response."""
    authorize_tenant(principal, payload.tenant_id)
    response = await service.create_provider(
        tenant_id=payload.tenant_id,
        name=payload.name,
        family=payload.family,
        config=payload.config,
        api_key=payload.api_key,
        actor=principal.subject,
    )
    return ProviderOut.from_domain(response.data["provider"])


@router.patch("/{provider_id}", response_model=ProviderOut, responses=CRUD_ERROR_RESPONSES)
async def update_provider(
    provider_id: str,
    payload: ProviderUpdateInput,
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> ProviderOut:
    """Update a provider; fields omitted from the request body are left unchanged."""
    response = await service.update_provider(
        provider_id,
        updates=payload.model_dump(exclude_unset=True),
        actor=principal.subject,
        tenant_ids=principal.tenant_scope,
    )
    return ProviderOut.from_domain(response.data["provider"])


@router.delete("/{provider_id}", status_code=204, responses=CRUD_ERROR_RESPONSES)
async def delete_provider(
    provider_id: str,
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> None:
    """Soft-delete a provider and forget its stored secret, if any."""
    await service.delete_provider(
        provider_id, actor=principal.subject, tenant_ids=principal.tenant_scope
    )


@router.post(
    "/{provider_id}/test",
    response_model=ProviderTestResult,
    responses=TEST_ERROR_RESPONSES,
)
async def test_provider_connection(
    provider_id: str,
    service: ProvidersServiceDependency,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> ProviderTestResult:
    """Make one small real call to a chat provider; 501 immediately for other families."""
    response = await service.test_provider_connection(
        provider_id, tenant_ids=principal.tenant_scope
    )
    return ProviderTestResult.from_domain(response.data["result"])
