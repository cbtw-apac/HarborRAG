from __future__ import annotations

import logging
from typing import Any

from pydantic import SecretStr

from harborrag_adapters.repositories.errors import MissingOptionalDependencyError
from harborrag_adapters.repositories.lifecycle import AsyncLifecycle
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig

try:
    from aiobotocore.config import AioConfig  # type: ignore[import-untyped]  # no stubs
    from aiobotocore.session import get_session  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    get_session = None

logger = logging.getLogger("harborrag.storage.s3")

_MAX_ATTEMPTS = 2
"""Tries per S3 request, the first included; callers' retry policies cover longer outages."""


class S3DBClient(AsyncLifecycle):
    """Owns one asynchronous S3-compatible SDK client and its context manager.

    A request that does not connect within ``connect_timeout_seconds``, or waits longer
    than ``operation_timeout_seconds`` for data, is tried at most ``_MAX_ATTEMPTS``
    times before botocore raises its timeout error.

    Args:
        config: Endpoint, region, credentials and timeouts of the object store.
    """

    def __init__(self, config: S3ObjectStoreConfig) -> None:
        if get_session is None:
            raise MissingOptionalDependencyError("aiobotocore")
        self._settings = {
            "endpoint_url": config.endpoint_url,
            "region_name": config.region,
            "aws_access_key_id": _secret_value(config.access_key_id),
            "aws_secret_access_key": _secret_value(config.secret_access_key),
            "aws_session_token": _secret_value(config.session_token),
        }
        self._connect_timeout_seconds = config.connect_timeout_seconds
        self._operation_timeout_seconds = config.operation_timeout_seconds
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
            session = get_session()
            kwargs = {key: value for key, value in self._settings.items() if value is not None}
            config = AioConfig(
                connect_timeout=self._connect_timeout_seconds,
                read_timeout=self._operation_timeout_seconds,
                retries={"total_max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
            )
            self._client_manager = session.create_client("s3", config=config, **kwargs)
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
                # exit -- log and swallow so it doesn't mask the original error.
                logger.debug("S3 client manager cleanup raised", exc_info=True)
        self._client_manager = None
        self._client = None

    async def ping(self) -> None:
        await self.raw.list_buckets()


def _secret_value(secret: SecretStr | None) -> str | None:
    """Return the secret's value for the SDK, or ``None`` when it is unset or empty."""
    return secret.get_secret_value() if secret else None
