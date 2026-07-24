from __future__ import annotations

from typing import Any

from ..security import SecretReference
from .base import parse_secret_reference


class AzureKeyVaultResolver:
    """Resolve Azure Key Vault secrets through injected clients or DefaultAzureCredential."""

    def __init__(
        self, clients: dict[str, Any] | None = None, *, credential: Any | None = None
    ) -> None:
        """Store clients by vault host and optional application-owned credential."""

        self._clients = dict(clients or {})
        self._credential = credential

    def resolve(self, reference: SecretReference) -> str:
        """Resolve `secret://azure/VAULT_HOST/SECRET_NAME[/VERSION]`."""

        parsed = parse_secret_reference(reference)
        if parsed.provider != "azure" or len(parsed.segments) not in {2, 3} or parsed.field:
            raise ValueError(
                "Azure secret URI must be secret://azure/VAULT_HOST/SECRET_NAME[/VERSION]"
            )
        vault_host, secret_name, *version = parsed.segments
        client = self._clients.get(vault_host) or self._build_client(vault_host)
        self._clients[vault_host] = client
        secret = client.get_secret(secret_name, version[0] if version else None)
        value = getattr(secret, "value", None)
        if not isinstance(value, str):
            raise ValueError("Azure secret response contains no string value")
        return value

    def _build_client(self, vault_host: str) -> Any:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as exc:
            raise RuntimeError(
                "Azure secret support requires azure-identity and azure-keyvault-secrets"
            ) from exc
        credential = self._credential or DefaultAzureCredential()
        return SecretClient(vault_url=f"https://{vault_host}", credential=credential)
