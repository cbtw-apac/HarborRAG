"""Port for finding the tenants that hold content.

The listing crosses tenants. Hand it only to an operator surface that has already
checked the caller may name any tenant. Reader tools never receive it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from harborrag_core.schemas.ids import TenantId


class TenantDirectory(Protocol):
    """Lists the tenants that own at least one source scope."""

    async def tenant_ids(self) -> list[TenantId]:
        """List the tenants that own a source scope, sorted and without repeats.

        An implementation may stop at a fixed number of tenants.

        Returns:
            The tenant IDs.

        Raises:
            HarborUnavailableError: The store cannot be reached or read.
        """
        ...
