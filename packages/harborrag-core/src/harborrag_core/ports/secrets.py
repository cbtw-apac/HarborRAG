"""SecretsPort: the only sanctioned path from a raw config value to a ref.

App/route layers never see a value this port resolves -- they store and
forward `ref` strings only. Only adapters/engine code that actually talks to
a connector may call `resolve()`.

Every operation is tenant-scoped: a ref belongs to exactly one tenant, and
`resolve`/`delete` must be handed the owning tenant. A ref presented with a
different `tenant_id` behaves exactly like an unknown ref, so leaking a ref
string across a tenant boundary leaks nothing.
"""

from __future__ import annotations

from typing import Protocol


class SecretsPort(Protocol):
    """Put/resolve/delete opaque secret references within one tenant."""

    async def put(self, value: str, *, tenant_id: str) -> str:
        """Store a raw value for a tenant and return an opaque ref; never logs the value."""

    async def resolve(self, ref: str, *, tenant_id: str) -> str:
        """Return the raw value behind one of the tenant's refs; raises otherwise."""

    async def delete(self, ref: str, *, tenant_id: str) -> None:
        """Forget the value behind one of the tenant's refs."""
