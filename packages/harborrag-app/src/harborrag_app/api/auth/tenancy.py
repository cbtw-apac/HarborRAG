"""Pick the tenant a request acts in.

The tenant comes from the credential. A credential that holds several tenants
names one with ``X-Tenant-Id``; a wildcard credential without the header acts
in the default tenant.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from harborrag_core.contracts.errors import HarborAuthError

from ..errors import TenantRequiredError
from .dependencies import get_principal
from .principal import Principal

DEFAULT_TENANT = "DEFAULT"


class TenantForbiddenError(HarborAuthError):
    """``X-Tenant-Id`` names a tenant the credential does not hold."""

    error_code = "tenant_forbidden"

    def __init__(self, message: str) -> None:
        super().__init__(message, forbidden=True)


def resolve_tenant(
    principal: Annotated[Principal, Depends(get_principal)],
    x_tenant_id: Annotated[
        str | None,
        Header(
            alias="X-Tenant-Id",
            description="Tenant to act in, when the credential holds several",
            max_length=128,
        ),
    ] = None,
) -> str:
    """Return the tenant this request acts in.

    Raises:
        TenantForbiddenError: The header names a tenant the credential does not hold.
        TenantRequiredError: The credential holds several tenants and sent no header.
    """
    if x_tenant_id:
        if not principal.can_access_tenant(x_tenant_id):
            raise TenantForbiddenError("the credential does not hold this tenant")
        return x_tenant_id
    if "*" in principal.tenant_ids:
        return DEFAULT_TENANT
    if len(principal.tenant_ids) == 1:
        return next(iter(principal.tenant_ids))
    raise TenantRequiredError("the credential holds several tenants; send X-Tenant-Id")


TenantDependency = Annotated[str, Depends(resolve_tenant)]
