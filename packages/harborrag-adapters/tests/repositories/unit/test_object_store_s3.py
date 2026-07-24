from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.errors import (
    HarborObjectTooLargeError,
    HarborStorageAlreadyExistsError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
)
from harborrag_adapters.repositories.object_store.keys import physical_object_key
from harborrag_adapters.repositories.object_store.s3 import (
    object_metadata as object_metadata_module,
)
from harborrag_adapters.repositories.object_store.s3 import (
    operations as operations_module,
)
from harborrag_adapters.repositories.object_store.s3 import (
    repository as repository_module,
)
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.repository import S3ObjectStore
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext


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


@pytest.fixture(autouse=True)
def fake_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(object_metadata_module, "ClientError", FakeClientError)
    monkeypatch.setattr(operations_module, "ClientError", FakeClientError)


def make_store(raw: FakeS3Raw) -> S3ObjectStore:
    return S3ObjectStore(
        S3ObjectStoreConfig(),
        client=FakeS3Client(raw),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_absent_key_always_uses_atomic_create_condition() -> None:
    raw = FakeS3Raw()
    store = make_store(raw)

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=b"value"),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert raw.put_calls[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in raw.put_calls[0]


@pytest.mark.asyncio
async def test_same_tenant_replacement_uses_observed_etag() -> None:
    raw = FakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"old-etag"'})
    store = make_store(raw)

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=b"replacement"),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert raw.put_calls[0]["IfMatch"] == '"old-etag"'
    assert "IfNoneMatch" not in raw.put_calls[0]


@pytest.mark.asyncio
async def test_async_body_uses_single_conditional_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = FakeS3Raw()
    store = make_store(raw)

    async def immediate(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(operations_module.asyncio, "to_thread", immediate)

    async def body():
        yield b"streamed-"
        yield b"value"

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=body()),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert len(raw.put_calls) == 1
    assert raw.put_calls[0]["IfNoneMatch"] == "*"
    assert raw.uploaded_body == b"streamed-value"
    assert store.capabilities.multipart_upload is False


@pytest.mark.asyncio
async def test_failed_write_condition_is_mapped_to_already_exists() -> None:
    raw = FakeS3Raw()
    raw.put_error = "PreconditionFailed"
    store = make_store(raw)

    with pytest.raises(HarborStorageAlreadyExistsError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_cross_tenant_object_is_never_sent_to_put_object() -> None:
    raw = FakeS3Raw({"Metadata": {"tenant_id": "tenant-b"}, "ETag": '"old-etag"'})
    store = make_store(raw)

    with pytest.raises(HarborStorageAlreadyExistsError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []


# --- Additional coverage below: get/head/exists/delete/list/presign, error
# mapping edge cases, and connect/close/health delegation. These extend the
# fixtures above via subclassing rather than editing them in place. ---


@pytest.fixture(autouse=True)
def fake_repository_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "ClientError", FakeClientError)


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


class _HugeChunk(bytes):
    """A one-byte payload that lies about its length to cheaply exercise the
    5 GB PutObject guard without allocating real memory."""

    def __len__(self) -> int:
        return 6_000_000_000


@pytest.mark.asyncio
async def test_put_without_bucket_and_no_default_raises_validation_error() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageValidationError):
        await store.put(
            PutObjectRequest(bucket="", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_put_sets_content_type_and_server_side_encryption() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw, config=S3ObjectStoreConfig(server_side_encryption="AES256"))

    await store.put(
        PutObjectRequest(bucket="bucket", key="key", body=b"value", content_type="text/plain"),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert raw.put_calls[0]["ContentType"] == "text/plain"
    assert raw.put_calls[0]["ServerSideEncryption"] == "AES256"


@pytest.mark.asyncio
async def test_put_replace_with_missing_etag_raises_already_exists() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": ""})
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageAlreadyExistsError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []


@pytest.mark.asyncio
async def test_put_checksum_mismatch_raises_validation_error() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageValidationError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value", checksum_sha256="0" * 64),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []


@pytest.mark.asyncio
async def test_put_unmapped_client_error_is_reraised() -> None:
    raw = ExtendedFakeS3Raw()
    raw.put_error = "InternalError"
    store = make_extended_store(raw)

    with pytest.raises(FakeClientError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_put_existing_head_check_reraises_unmapped_client_error() -> None:
    raw = ExtendedFakeS3Raw()
    raw.head_error = "InternalError"
    store = make_extended_store(raw)

    with pytest.raises(FakeClientError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=b"value"),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )


@pytest.mark.asyncio
async def test_put_rejects_object_larger_than_five_gigabytes() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw)

    async def huge_body():
        yield _HugeChunk(b"x")

    with pytest.raises(HarborObjectTooLargeError):
        await store.put(
            PutObjectRequest(bucket="bucket", key="key", body=huge_body()),
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    assert raw.put_calls == []


@pytest.mark.asyncio
async def test_get_bytes_without_range_reads_entire_object() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    raw.object_body = b"hello world"
    store = make_extended_store(raw)

    data = await store.get_bytes(
        "bucket",
        "key",
        byte_range=None,
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert data == b"hello world"
    assert "Range" not in raw.get_object_calls[0]


@pytest.mark.asyncio
async def test_get_bytes_with_byte_range_sets_range_header() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    raw.object_body = b"hello world"
    store = make_extended_store(raw)

    await store.get_bytes(
        "bucket",
        "key",
        byte_range=(0, 4),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert raw.get_object_calls[0]["Range"] == "bytes=0-4"


@pytest.mark.asyncio
async def test_iter_bytes_streams_in_chunks() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    raw.object_body = b"abcdefghij"
    store = make_extended_store(raw)

    chunks = [
        chunk
        async for chunk in store.iter_bytes(
            "bucket",
            "key",
            chunk_size=4,
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
    ]

    assert chunks == [b"abcd", b"efgh", b"ij"]


@pytest.mark.asyncio
async def test_head_returns_normalized_metadata() -> None:
    raw = ExtendedFakeS3Raw(
        {
            "Metadata": {"tenant_id": "tenant-a", "custom": "1"},
            "ETag": '"etag-value"',
            "ContentLength": 42,
            "ContentType": "text/plain",
        }
    )
    store = make_extended_store(raw)

    metadata = await store.head(
        "bucket", "key", context=StorageOperationContext(tenant_id="tenant-a")
    )

    assert metadata.reference.etag == "etag-value"
    assert metadata.reference.size_bytes == 42
    assert metadata.reference.content_type == "text/plain"
    assert metadata.metadata["custom"] == "1"


@pytest.mark.asyncio
async def test_exists_true_for_owned_object_false_otherwise() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")

    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    store = make_extended_store(raw)
    assert await store.exists("bucket", "key", context=context) is True

    raw_absent = ExtendedFakeS3Raw()
    store_absent = make_extended_store(raw_absent)
    assert await store_absent.exists("bucket", "key", context=context) is False


@pytest.mark.asyncio
async def test_delete_true_when_present_false_when_absent() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")

    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    store = make_extended_store(raw)
    assert await store.delete("bucket", "key", context=context) is True
    assert len(raw.delete_calls) == 1

    raw_absent = ExtendedFakeS3Raw()
    store_absent = make_extended_store(raw_absent)
    assert await store_absent.delete("bucket", "key", context=context) is False
    assert raw_absent.delete_calls == []


@pytest.mark.asyncio
async def test_list_paginates_and_filters_foreign_tenant_and_missing_items() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})

    missing_physical = physical_object_key("tenant-a", "docs/missing")
    raw.head_by_key[missing_physical] = None
    raw.list_pages = [
        {
            "Contents": [
                {"Key": physical_object_key("tenant-a", "docs/a")},
                {"Key": missing_physical},
                {"Key": physical_object_key("tenant-b", "docs/x")},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "token-1",
        },
        {
            "Contents": [{"Key": physical_object_key("tenant-a", "docs/b")}],
            "IsTruncated": False,
        },
    ]
    store = make_extended_store(raw)

    results = await store.list("bucket", "docs/", limit=10, context=context)

    assert {item.reference.key for item in results} == {"docs/a", "docs/b"}


@pytest.mark.asyncio
async def test_presign_download_includes_version_id_when_present() -> None:
    raw = ExtendedFakeS3Raw(
        {"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"', "VersionId": "v1"}
    )
    store = make_extended_store(raw)

    url = await store.presign_download(
        "bucket",
        "key",
        expires_seconds=60,
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert url.startswith("https://example.test/bucket/")
    assert raw.presign_calls[0][1]["VersionId"] == "v1"
    assert raw.presign_calls[0][2] == 60


@pytest.mark.asyncio
async def test_presign_download_omits_version_id_when_absent() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-a"}, "ETag": '"e"'})
    store = make_extended_store(raw)

    await store.presign_download(
        "bucket",
        "key",
        expires_seconds=60,
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert "VersionId" not in raw.presign_calls[0][1]


@pytest.mark.asyncio
async def test_connect_close_and_health_delegate_to_database() -> None:
    raw = ExtendedFakeS3Raw()
    client = FakeS3ClientWithLifecycle(raw)
    store = S3ObjectStore(S3ObjectStoreConfig(), client=client)  # type: ignore[arg-type]

    await store.connect()
    assert client.connected is True

    health = await store.health()
    assert health.status == HealthStatus.HEALTHY

    await store.close()
    assert client.connected is False


@pytest.mark.asyncio
async def test_authorize_unmapped_client_error_is_reraised() -> None:
    raw = ExtendedFakeS3Raw()
    raw.head_error = "InternalError"
    store = make_extended_store(raw)

    with pytest.raises(FakeClientError):
        await store.exists("bucket", "key", context=StorageOperationContext(tenant_id="tenant-a"))


@pytest.mark.asyncio
async def test_authorize_cross_tenant_object_is_not_found() -> None:
    raw = ExtendedFakeS3Raw({"Metadata": {"tenant_id": "tenant-b"}, "ETag": '"e"'})
    store = make_extended_store(raw)

    with pytest.raises(HarborStorageNotFoundError):
        await store.get_bytes(
            "bucket",
            "key",
            byte_range=None,
            context=StorageOperationContext(tenant_id="tenant-a"),
        )
