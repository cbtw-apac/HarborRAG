from __future__ import annotations

from typing import Any

try:
    from botocore.exceptions import ClientError  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    ClientError = Exception


class S3ObjectMetadataMixin:
    """Resolve optional S3 object metadata without owning write behavior."""

    client: Any

    async def _existing_head(self, bucket: str, key: str) -> dict[str, Any] | None:
        try:
            response: dict[str, Any] = await self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if self._client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return response

    @staticmethod
    def _client_error_code(exc: Exception) -> str:
        response = getattr(exc, "response", {})
        return str(response.get("Error", {}).get("Code", ""))
