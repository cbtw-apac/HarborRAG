from __future__ import annotations

import json
from typing import Any

from ..security import SecretReference
from .base import parse_secret_reference


class AwsSecretsManagerResolver:
    """Resolve AWS Secrets Manager values through an injected or lazy boto3 client."""

    def __init__(self, client: Any | None = None, *, region_name: str | None = None) -> None:
        """Store an optional client and region without resolving credentials eagerly."""

        self._client = client
        self._region_name = region_name

    def resolve(self, reference: SecretReference) -> str:
        """Resolve `secret://aws/secret-id#json-field` without logging secret content."""

        parsed = parse_secret_reference(reference)
        if parsed.provider != "aws" or not parsed.segments:
            raise ValueError("AWS secret URI must be secret://aws/SECRET_ID[#FIELD]")
        secret_id = "/".join(parsed.segments)
        response = self._get_client().get_secret_value(SecretId=secret_id)
        value = response.get("SecretString")
        if value is None:
            binary = response.get("SecretBinary")
            if binary is None:
                raise ValueError("AWS secret response contains no secret value")
            value = binary.decode() if isinstance(binary, bytes) else str(binary)
        return _select_json_field(str(value), parsed.field)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("AWS secret support requires boto3") from exc
            self._client = boto3.client("secretsmanager", region_name=self._region_name)
        return self._client


def _select_json_field(value: str, field: str | None) -> str:
    if field is None:
        return value
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or field not in parsed:
        raise KeyError(f"secret JSON field is not available: {field}")
    selected = parsed[field]
    return selected if isinstance(selected, str) else json.dumps(selected, separators=(",", ":"))
