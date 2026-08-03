from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store import ARTIFACT_BUCKET, RAW_BUCKET

from .fakes import ExtendedFakeS3Raw, make_extended_store


@pytest.mark.asyncio
async def test_bucket_provisioning_is_idempotent() -> None:
    raw = ExtendedFakeS3Raw()
    store = make_extended_store(raw)

    await store.ensure_buckets((RAW_BUCKET, ARTIFACT_BUCKET))
    await store.ensure_buckets((RAW_BUCKET, ARTIFACT_BUCKET))

    assert raw.buckets == {RAW_BUCKET, ARTIFACT_BUCKET}
    assert [call["Bucket"] for call in raw.create_bucket_calls] == [
        RAW_BUCKET,
        ARTIFACT_BUCKET,
    ]
