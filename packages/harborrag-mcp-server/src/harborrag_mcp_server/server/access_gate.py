"""The beta access file's door check, shared by every MCP entry to a tool.

With an access file in force, a principal it does not list is refused before any tool
runs: the retrieval facades would refuse its reads anyway, but a tool is free to turn a
failed read into a result, and the call would still be served from the model's own words.
Without a listing there is no access file, and nothing is checked.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

REFUSAL = "not listed in the access file"


class TenantListing(Protocol):
    """What the check reads from the access file: where a principal is listed."""

    def listed_tenants(self, principal_id: str) -> frozenset[str]:
        """The tenants in which ``principal_id`` may read at least one connection."""
        ...


def require_listed(listing: TenantListing | None, principal_id: str, tenant_id: str | None) -> None:
    """Raise ``PermissionError`` unless the file lists ``principal_id`` in ``tenant_id``.

    A call that binds no tenant (``describe_graph`` reads none) needs the principal to be
    listed somewhere. The listing stats the file and may parse it, so async callers use
    :func:`require_listed_off_loop`.
    """

    if listing is None:
        return
    listed = listing.listed_tenants(principal_id)
    if tenant_id is None:
        permitted = bool(listed)
    else:
        permitted = tenant_id in listed
    if not permitted:
        raise PermissionError(REFUSAL)


async def require_listed_off_loop(
    listing: TenantListing | None, principal_id: str, tenant_id: str | None
) -> None:
    """:func:`require_listed` for async callers: the file is read in a worker thread."""

    if listing is not None:
        await asyncio.to_thread(require_listed, listing, principal_id, tenant_id)
