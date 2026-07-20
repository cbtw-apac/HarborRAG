"""Secrets port (plan §8.2): write-only from the app layer's perspective.

resolve() exists on the port because adapters/engine need it, but no app-layer
code may ever call it — the app service surface exposes put() only.
"""

from __future__ import annotations

from typing import Protocol


class SecretsPort(Protocol):
    """Store secret values behind opaque refs; refs are safe to persist/log."""

    async def put(self, value: str) -> str:
        """Store a secret value; returns its opaque secret_ref."""

    async def resolve(self, ref: str) -> str:
        """Return the secret value for a ref (adapters/engine only)."""

    async def delete(self, ref: str) -> None:
        """Forget a secret; subsequent resolve() for the ref must fail."""
