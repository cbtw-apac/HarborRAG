from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.lifecycle import AsyncLifecycle

try:
    import aioboto3  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    aioboto3 = None


class S3DBClient(AsyncLifecycle):
    """Owns one asynchronous S3-compatible SDK client and its context manager."""

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        region: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
        session_token: str | None,
    ) -> None:
        if aioboto3 is None:
            raise ImportError("aioboto3 is not installed")
        self._settings = {
            "endpoint_url": endpoint_url,
            "region_name": region,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "aws_session_token": session_token,
        }
        self._client_manager: Any = None
        self._client: Any = None

    @property
    def backend(self) -> str:
        return "s3"

    @property
    def raw(self) -> Any:
        if self._client is None:
            raise RuntimeError("S3 client is not connected")
        return self._client

    async def connect(self) -> None:
        if self._client is not None:
            return
        try:
            session = aioboto3.Session()
            kwargs = {key: value for key, value in self._settings.items() if value is not None}
            self._client_manager = session.client("s3", **kwargs)
            self._client = await self._client_manager.__aenter__()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._client_manager is not None:
            try:
                await self._client_manager.__aexit__(None, None, None)
            except Exception:
                # Best-effort cleanup: if connect() failed before __aenter__()
                # fully completed, the manager may not have a live client to
                # exit -- swallow that so it doesn't mask the original error.
                pass
        self._client_manager = None
        self._client = None

    async def ping(self) -> None:
        await self.raw.list_buckets()
