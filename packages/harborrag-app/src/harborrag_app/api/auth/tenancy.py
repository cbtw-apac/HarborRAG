"""Pick the tenant a request acts in.

The tenant comes from the credential. A credential that holds several tenants
names one with ``X-Tenant-Id``; a wildcard credential without the header acts
in the tenant ``HARBORRAG_TENANT_ID`` names, which the API keeps in
``app.state.default_tenant_id``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from harborrag_app.operations.errors import TenantForbiddenError, TenantRequiredError

from .dependencies import get_principal
from .principal import Principal

TenantHeader = Annotated[
    str | None,
    Header(
        alias="X-Tenant-Id",
        description="Tenant to act in, when the credential holds several",
        max_length=128,
    ),
]


def tenant_header(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    x_tenant_id: TenantHeader = None,
) -> str | None:
    """Return the tenant ``X-Tenant-Id`` names, or the credential's only one.

    A wildcard credential without the header gets the configured default tenant.

    Returns:
        The tenant, or ``None`` when the credential holds several and sent no header.

    Raises:
        TenantForbiddenError: The header names a tenant the credential does not hold.
    """
    if x_tenant_id:
        if not principal.can_access_tenant(x_tenant_id):
            raise TenantForbiddenError()
        return x_tenant_id
    if "*" in principal.tenant_ids:
        return str(request.app.state.default_tenant_id)
    if len(principal.tenant_ids) == 1:
        return next(iter(principal.tenant_ids))
    return None


def resolve_tenant(tenant: Annotated[str | None, Depends(tenant_header)]) -> str:
    """Return the tenant this request acts in.

    Raises:
        TenantForbiddenError: The header names a tenant the credential does not hold.
        TenantRequiredError: The credential holds several tenants and sent no header.
    """
    if tenant is None:
        raise TenantRequiredError("the credential holds several tenants; send X-Tenant-Id")
    return tenant


TenantDependency = Annotated[str, Depends(resolve_tenant)]
OptionalTenantDependency = Annotated[str | None, Depends(tenant_header)]
