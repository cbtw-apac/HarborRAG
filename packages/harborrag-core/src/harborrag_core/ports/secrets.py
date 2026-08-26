"""SecretsPort: the only sanctioned path from a raw config value to a ref.

App/route layers never see a value this port resolves -- they store and
forward `ref` strings only. Only adapters/engine code that actually talks to
a connector may call `resolve()`.
"""

from __future__ import annotations

from typing import Protocol


class SecretsPort(Protocol):
    """Put/resolve/delete opaque secret references."""

    async def put(self, value: str) -> str:
        """Store a raw value and return an opaque ref; never logs the value."""

    async def resolve(self, ref: str) -> str:
        """Return the raw value behind a ref; raises for an unknown/deleted ref."""

    async def delete(self, ref: str) -> None:
        """Forget the value behind a ref."""
