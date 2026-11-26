from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.object_store.s3 import S3ObjectStore, S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3 import client as client_module

pytestmark = pytest.mark.unit


class _Manager:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _RecordingSession:
    def __init__(self) -> None:
        self.client_kwargs: dict[str, Any] = {}

    def create_client(self, service_name: str, **kwargs: Any) -> _Manager:
        assert service_name == "s3"
        self.client_kwargs = kwargs
        return _Manager()


@pytest.mark.asyncio
async def test_s3_store_hands_its_timeouts_and_a_retry_bound_to_botocore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _RecordingSession()
    monkeypatch.setattr(client_module, "get_session", lambda: session)
    store = S3ObjectStore(
        S3ObjectStoreConfig(
            endpoint_url="http://localhost:9000",
            connect_timeout_seconds=4,
            operation_timeout_seconds=7,
        )
    )

    await store.connect()
    await store.close()

    config = session.client_kwargs["config"]
    assert config.connect_timeout == 4
    assert config.read_timeout == 7
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
    assert session.client_kwargs["endpoint_url"] == "http://localhost:9000"
