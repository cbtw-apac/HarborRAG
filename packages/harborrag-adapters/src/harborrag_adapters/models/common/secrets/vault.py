from __future__ import annotations

import json
from typing import Any

from ..security import SecretReference
from .base import parse_secret_reference


class VaultSecretResolver:
    """Resolve HashiCorp Vault KV v2 values through an injected or lazy hvac client."""

    def __init__(self, client: Any | None = None, *, mount_point: str = "secret") -> None:
        """Store the Vault client and KV mount point."""

        self._client = client
        self._mount_point = mount_point

    def resolve(self, reference: SecretReference) -> str:
        """Resolve `secret://vault/PATH#FIELD` from Vault KV v2."""

        parsed = parse_secret_reference(reference)
        if parsed.provider != "vault" or not parsed.segments or not parsed.field:
            raise ValueError("Vault secret URI must be secret://vault/PATH#FIELD")
        response = self._get_client().secrets.kv.v2.read_secret_version(
            path="/".join(parsed.segments), mount_point=self._mount_point
        )
        values = response.get("data", {}).get("data", {})
        if parsed.field not in values:
            raise KeyError(f"Vault secret field is not available: {parsed.field}")
        value = values[parsed.field]
        return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import hvac
            except ImportError as exc:
                raise RuntimeError("Vault secret support requires hvac") from exc
            self._client = hvac.Client()
        return self._client
