"""Pick the tenant a request acts in.

The tenant comes from the credential. A credential that holds several tenants
names one with ``X-Tenant-Id``; a wildcard credential without the header acts
in the default tenant.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from ..errors import TenantRequiredError
from .dependencies import TenantForbiddenError, get_principal
from .principal import Principal

DEFAULT_TENANT = "DEFAULT"

TenantHeader = Annotated[
    str | None,
    Header(
        alias="X-Tenant-Id",
        description="Tenant to act in, when the credential holds several",
        max_length=128,
    ),
]


def tenant_header(
    principal: Annotated[Principal, Depends(get_principal)],
    x_tenant_id: TenantHeader = None,
) -> str | None:
    """Return the tenant ``X-Tenant-Id`` names, or the credential's only one.

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
        return DEFAULT_TENANT
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
