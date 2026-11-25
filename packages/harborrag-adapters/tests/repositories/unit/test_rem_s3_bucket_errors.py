"""Bucket provisioning and validation raise the same storage errors as object operations."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from harborrag_adapters.repositories.errors import (
    HarborStorageConnectionError,
    HarborStorageError,
    HarborStorageTimeoutError,
)
from harborrag_adapters.repositories.object_store import ARTIFACT_BUCKET, RAW_BUCKET

from .test_object_store_s3.fakes import ExtendedFakeS3Raw, make_extended_store

pytestmark = pytest.mark.unit

ENDPOINT = "http://minio.invalid:9000"


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadBucket"
    )


class _BucketFailureRaw(ExtendedFakeS3Raw):
    """Raw S3 client whose bucket calls fail with the given errors."""

    def __init__(
        self, *, head_error: Exception | None = None, create_error: Exception | None = None
    ) -> None:
        super().__init__()
        self.head_bucket_error = head_error
        self.create_bucket_error = create_error

    async def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        if self.head_bucket_error is not None:
            raise self.head_bucket_error
        return await super().head_bucket(**kwargs)

    async def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        if self.create_bucket_error is not None:
            raise self.create_bucket_error
        return await super().create_bucket(**kwargs)


_TRANSLATED = [
    (ConnectTimeoutError(endpoint_url=ENDPOINT), HarborStorageTimeoutError),
    (ReadTimeoutError(endpoint_url=ENDPOINT), HarborStorageTimeoutError),
    (EndpointConnectionError(endpoint_url=ENDPOINT), HarborStorageConnectionError),
    (_client_error("ServiceUnavailable", 503), HarborStorageConnectionError),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "expected"), _TRANSLATED)
async def test_ensure_buckets_head_failure_becomes_a_storage_error(
    error: Exception, expected: type[HarborStorageError]
) -> None:
    store = make_extended_store(_BucketFailureRaw(head_error=error))

    with pytest.raises(expected) as raised:
        await store.ensure_buckets((RAW_BUCKET, ARTIFACT_BUCKET))

    assert raised.value.__cause__ is error
    assert raised.value.context.retryable is True
    assert raised.value.context.operation == "ensure_buckets"
    assert raised.value.context.tenant_id is None
    assert RAW_BUCKET not in str(raised.value)


@pytest.mark.asyncio
async def test_ensure_buckets_create_timeout_becomes_a_storage_error() -> None:
    error = ReadTimeoutError(endpoint_url=ENDPOINT)
    raw = _BucketFailureRaw(head_error=_client_error("NoSuchBucket", 404), create_error=error)

    with pytest.raises(HarborStorageTimeoutError) as raised:
        await make_extended_store(raw).ensure_buckets((RAW_BUCKET,))

    assert raised.value.__cause__ is error


@pytest.mark.asyncio
async def test_ensure_buckets_access_denied_passes_through() -> None:
    raw = _BucketFailureRaw(head_error=_client_error("AccessDenied", 403))

    with pytest.raises(ClientError):
        await make_extended_store(raw).ensure_buckets((RAW_BUCKET,))


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "expected"), _TRANSLATED)
async def test_validate_buckets_failure_becomes_a_storage_error(
    error: Exception, expected: type[HarborStorageError]
) -> None:
    store = make_extended_store(_BucketFailureRaw(head_error=error))

    with pytest.raises(expected) as raised:
        await store.validate_buckets((ARTIFACT_BUCKET,))

    assert raised.value.__cause__ is error
    assert raised.value.context.operation == "validate_buckets"
    assert ARTIFACT_BUCKET not in str(raised.value)


@pytest.mark.asyncio
async def test_validate_buckets_missing_bucket_stays_a_plain_runtime_error() -> None:
    raw = _BucketFailureRaw(head_error=_client_error("NoSuchBucket", 404))

    with pytest.raises(RuntimeError, match="required reader bucket") as raised:
        await make_extended_store(raw).validate_buckets((ARTIFACT_BUCKET,))

    assert not isinstance(raised.value, HarborStorageError)
