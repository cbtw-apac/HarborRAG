from __future__ import annotations

from typing import Any

from ..security import SecretReference
from .base import parse_secret_reference


class GoogleSecretManagerResolver:
    """Resolve Google Secret Manager versions through an injected or lazy client."""

    def __init__(self, client: Any | None = None) -> None:
        """Store an optional application-owned Secret Manager client."""

        self._client = client

    def resolve(self, reference: SecretReference) -> str:
        """Resolve `secret://gcp/PROJECT/SECRET[/VERSION]`."""

        parsed = parse_secret_reference(reference)
        if parsed.provider != "gcp" or len(parsed.segments) not in {2, 3} or parsed.field:
            raise ValueError("GCP secret URI must be secret://gcp/PROJECT/SECRET[/VERSION]")
        project, secret, *version = parsed.segments
        name = f"projects/{project}/secrets/{secret}/versions/{version[0] if version else 'latest'}"
        response = self._get_client().access_secret_version(request={"name": name})
        data = response.payload.data
        return data.decode() if isinstance(data, bytes) else str(data)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise RuntimeError(
                    "GCP secret support requires google-cloud-secret-manager"
                ) from exc
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client
