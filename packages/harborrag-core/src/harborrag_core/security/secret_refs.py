"""Secret references held in connection credentials.

Handles:
- ``SecretRef``: ``secret://…`` (stored through ``SecretsPort``), ``env:NAME``
  and ``vault:<path>`` pointers
- ``StoredSecret``: the stored form ``{"secret_ref", "last4", "updated_at"}``
- ``secret_status`` and ``credentials_view``: the read-side ``SecretStatus``

Does NOT resolve a reference to its value; the runtime resolvers own that.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

SecretSource = Literal["stored", "env", "vault"]

STORED_PREFIX = "secret://"
ENV_PREFIX = "env:"
VAULT_PREFIX = "vault:"
REFERENCE_KEY = "secret_ref"
STORED_KEYS = frozenset({REFERENCE_KEY, "last4", "updated_at"})
LAST4_MIN_LENGTH = 8

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A pointer to a secret value kept outside the connection row."""

    value: str

    @classmethod
    def parse(cls, raw: str) -> SecretRef:
        """Parse ``secret://…``, ``env:NAME`` or ``vault:<path>``.

        Args:
            raw: The reference string.

        Returns:
            The parsed reference.

        Raises:
            ValueError: The scheme is unknown or the remainder is empty or malformed.
        """
        value = raw.strip()
        if value.startswith(ENV_PREFIX):
            if not _ENV_NAME.fullmatch(value.removeprefix(ENV_PREFIX)):
                raise ValueError("env: references must name an environment variable")
            return cls(value)
        for prefix in (STORED_PREFIX, VAULT_PREFIX):
            if value.startswith(prefix) and len(value) > len(prefix):
                return cls(value)
        raise ValueError("secret_ref must start with secret://, env: or vault:")

    @property
    def source(self) -> SecretSource:
        """Where the value lives, as reported in ``SecretStatus.from``."""
        if self.value.startswith(ENV_PREFIX):
            return "env"
        if self.value.startswith(VAULT_PREFIX):
            return "vault"
        return "stored"

    @property
    def target(self) -> str:
        """The reference without its scheme, e.g. the environment variable name."""
        for prefix in (ENV_PREFIX, VAULT_PREFIX):
            if self.value.startswith(prefix):
                return self.value.removeprefix(prefix)
        return self.value

    @property
    def is_stored(self) -> bool:
        """True when HarborRAG owns the value and must delete it when replaced."""
        return self.source == "stored"


@dataclass(frozen=True, slots=True)
class StoredSecret:
    """The stored form of one secret credential field."""

    ref: SecretRef
    last4: str | None
    updated_at: str | None

    @classmethod
    def for_value(cls, ref: SecretRef, value: str, updated_at: datetime) -> StoredSecret:
        """Describe a value written through ``SecretsPort``; short values keep no ``last4``."""
        last4 = value[-4:] if len(value) >= LAST4_MIN_LENGTH else None
        return cls(ref, last4, updated_at.isoformat())

    @classmethod
    def for_reference(cls, ref: SecretRef, updated_at: datetime) -> StoredSecret:
        """Describe an external reference; HarborRAG never sees its value."""
        return cls(ref, None, updated_at.isoformat())

    @classmethod
    def from_json(cls, value: object) -> StoredSecret | None:
        """Read a stored field; ``None`` when ``value`` is not secret-shaped."""
        if not is_secret_shaped(value):
            return None
        value = cast(Mapping[str, Any], value)
        last4 = value.get("last4")
        updated_at = value.get("updated_at")
        return cls(
            SecretRef(str(value[REFERENCE_KEY])),
            str(last4) if last4 is not None else None,
            str(updated_at) if updated_at is not None else None,
        )

    def to_json(self) -> dict[str, Any]:
        """Return the shape persisted in ``credentials``."""
        return {REFERENCE_KEY: self.ref.value, "last4": self.last4, "updated_at": self.updated_at}

    def status(self) -> dict[str, Any]:
        """Return the API's ``SecretStatus`` for this field."""
        return {
            "set": True,
            "last4": self.last4 if self.ref.is_stored else None,
            "updated_at": self.updated_at,
            "from": self.ref.source,
        }


def is_secret_shaped(value: object) -> bool:
    """True for ``{"secret_ref": str, …}`` holding only stored-form keys."""
    return (
        isinstance(value, Mapping)
        and REFERENCE_KEY in value
        and isinstance(value[REFERENCE_KEY], str)
        and set(value) <= STORED_KEYS
    )


def secret_status(value: object) -> dict[str, Any] | None:
    """Return the ``SecretStatus`` of a stored field, or ``None`` when it is not a secret."""
    stored = StoredSecret.from_json(value)
    return stored.status() if stored else None


def credentials_view(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Return credentials as clients may read them: every secret as its ``SecretStatus``."""
    return {
        key: status if (status := secret_status(value)) is not None else value
        for key, value in credentials.items()
    }


def stored_refs(credentials: Mapping[str, Any]) -> list[str]:
    """Return the refs in ``credentials`` whose values HarborRAG owns."""
    refs = []
    for value in credentials.values():
        stored = StoredSecret.from_json(value)
        if stored is not None and stored.ref.is_stored:
            refs.append(stored.ref.value)
    return refs


def all_refs(credentials: Mapping[str, Any]) -> list[str]:
    """Return every secret reference in ``credentials``."""
    return [
        stored.ref.value
        for value in credentials.values()
        if (stored := StoredSecret.from_json(value)) is not None
    ]
