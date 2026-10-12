"""Where connector credentials live, resolved inside activities by connection ID.

Workflow inputs carry IDs only. An activity asks the store for a credential
when it builds a connector, so secrets never enter Temporal history.

Rotating OAuth refresh tokens need care: when two workers refresh the same
grant, the loser's refresh token is already revoked. Stores therefore version
every credential, and writers swap with :meth:`CredentialStore.compare_and_swap`,
re-reading on conflict instead of refreshing again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Credential:
    """A named set of secret values plus a version for compare-and-swap."""

    values: Mapping[str, str] = field(repr=False)
    version: int = 0

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("credential version must not be negative")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def require(self, name: str) -> str:
        """Return one value, raising ``KeyError`` naming only the missing field."""

        value = self.values.get(name)
        if not value:
            raise KeyError(f"credential value {name!r} is missing")
        return value


class CredentialStore(Protocol):
    """Read and atomically replace connector credentials."""

    async def get(self, connection_id: str, name: str) -> Credential:
        """Return the current credential; raise ``KeyError`` when there is none."""
        ...

    async def compare_and_swap(
        self,
        connection_id: str,
        name: str,
        *,
        expected_version: int,
        values: Mapping[str, str],
    ) -> Credential | None:
        """Store ``values`` if the stored version is still ``expected_version``.

        Return the new credential, or ``None`` when another writer won the race;
        the caller then re-reads with :meth:`get`.
        """
        ...
