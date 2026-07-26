from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.repository import S3ObjectStore


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Raw:
    def __init__(self, head: dict[str, Any] | None = None) -> None:
        self.head = head
        self.put_calls: list[dict[str, Any]] = []
        self.put_error: str | None = None
        self.uploaded_body: bytes | None = None

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if self.head is None:
            raise FakeClientError("404")
        return self.head

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        body = kwargs["Body"]
        self.uploaded_body = body if isinstance(body, bytes) else body.read()
        if self.put_error:
            raise FakeClientError(self.put_error)
        return {"ETag": '"new-etag"'}


class FakeS3Client:
    def __init__(self, raw: FakeS3Raw) -> None:
        self.raw = raw


def make_store(raw: FakeS3Raw) -> S3ObjectStore:
    return S3ObjectStore(
        S3ObjectStoreConfig(),
        client=FakeS3Client(raw),  # type: ignore[arg-type]
    )


class FakeAsyncBody:
    """Minimal async context-managed stream mimicking aioboto3's StreamingBody."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    async def __aenter__(self) -> FakeAsyncBody:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def read(self, size: int | None = None) -> bytes:
        if size is None:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class ExtendedFakeS3Raw(FakeS3Raw):
    """Adds read/list/delete/presign faking plus per-key head overrides."""

    def __init__(self, head: dict[str, Any] | None = None) -> None:
        super().__init__(head)
        self.head_error: str | None = None
        self.head_by_key: dict[str, dict[str, Any] | None] = {}
        self.object_body: bytes = b""
        self.get_object_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.list_pages: list[dict[str, Any]] = [{"Contents": [], "IsTruncated": False}]
        self.presign_calls: list[tuple[str, dict[str, Any], int]] = []

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.head_error:
            raise FakeClientError(self.head_error)
        key = kwargs.get("Key")
        if key in self.head_by_key:
            response = self.head_by_key[key]
            if response is None:
                raise FakeClientError("404")
            return response
        return await super().head_object(**kwargs)

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_object_calls.append(kwargs)
        return {"Body": FakeAsyncBody(self.object_body)}

    async def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(kwargs)
        return {}

    async def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if self.list_pages:
            return self.list_pages.pop(0)
        return {"Contents": [], "IsTruncated": False}

    async def generate_presigned_url(
        self, operation: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        self.presign_calls.append((operation, Params, ExpiresIn))
        return f"https://example.test/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"


class FakeS3ClientWithLifecycle(FakeS3Client):
    """Adds connect/close/ping so store.connect()/close()/health() are testable."""

    def __init__(self, raw: FakeS3Raw) -> None:
        super().__init__(raw)
        self.connected = False
        self.ping_error: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def ping(self) -> None:
        if self.ping_error is not None:
            raise self.ping_error


def make_extended_store(
    raw: ExtendedFakeS3Raw, *, config: S3ObjectStoreConfig | None = None
) -> S3ObjectStore:
    return S3ObjectStore(
        config or S3ObjectStoreConfig(),
        client=FakeS3ClientWithLifecycle(raw),  # type: ignore[arg-type]
    )


class HugeChunk(bytes):
    """A one-byte payload that lies about its length to cheaply exercise the
    5 GB PutObject guard without allocating real memory."""

    def __len__(self) -> int:
        return 6_000_000_000
