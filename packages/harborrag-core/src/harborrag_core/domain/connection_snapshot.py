"""Immutable, secret-free snapshot of a connection as one ingestion run uses it.

Handles:
- the content a connector is built from: type, source key, credentials with
  secrets as references, config and merged tuning
- the content fingerprint every activity of the run checks against
- the stored secret references the snapshot depends on
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from harborrag_core.security.secret_refs import REFERENCE_KEY, StoredSecret

from .validation import require_id, require_tenant_id, validate_secret_free_config

FINGERPRINT_PREFIX = "connection-"


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """What one run builds its connector from; never holds a secret value."""

    tenant_id: str
    source_key: str
    source_type: str
    credentials: Mapping[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    tuning: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_tenant_id(self.tenant_id)
        require_id(self.source_key, label="Source key")
        require_id(self.source_type, label="Source type")
        validate_secret_free_config(dict(self.credentials))

    @property
    def fingerprint(self) -> str:
        """Hash of the content only, so an identical write keeps the same fingerprint."""
        digest = sha256(_canonical(self.content()).encode("utf-8")).hexdigest()
        return f"{FINGERPRINT_PREFIX}{digest[:32]}"

    @property
    def secret_refs(self) -> list[str]:
        """Every secret reference the snapshot's credentials point at."""
        return [
            stored.ref.value
            for value in self.credentials.values()
            if (stored := StoredSecret.from_json(value)) is not None
        ]

    def content(self) -> dict[str, Any]:
        """Return the hashed content: secret fields reduced to their reference."""
        return {
            "tenant_id": self.tenant_id,
            "source_key": self.source_key,
            "source_type": self.source_type,
            "credentials": {
                key: {REFERENCE_KEY: stored.ref.value}
                if (stored := StoredSecret.from_json(value)) is not None
                else value
                for key, value in self.credentials.items()
            },
            "config": dict(self.config),
            "tuning": dict(self.tuning),
        }

    def to_json(self) -> dict[str, Any]:
        """Return the persisted form; secrets stay as references."""
        return {
            "tenant_id": self.tenant_id,
            "source_key": self.source_key,
            "source_type": self.source_type,
            "credentials": dict(self.credentials),
            "config": dict(self.config),
            "tuning": dict(self.tuning),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ConnectionSnapshot:
        """Rebuild a snapshot persisted with ``to_json``."""
        return cls(
            tenant_id=str(value["tenant_id"]),
            source_key=str(value["source_key"]),
            source_type=str(value["source_type"]),
            credentials=dict(value.get("credentials") or {}),
            config=dict(value.get("config") or {}),
            tuning=dict(value.get("tuning") or {}),
        )


def _canonical(value: Any) -> str:
    """Serialize deterministically for hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
