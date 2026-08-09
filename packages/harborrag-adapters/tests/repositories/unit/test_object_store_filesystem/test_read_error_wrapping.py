from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.errors import HarborStorageError, HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_core.schemas.object_store import ObjectMetadata, PutObjectRequest

from .conftest import make_context


@pytest.mark.asyncio
async def test_get_bytes_range_os_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        original_open = store._open_regular_file
        calls = {"count": 0}

        def boom_open(path: Path) -> object:
            calls["count"] += 1
            if calls["count"] > 1:
                raise OSError("boom")
            return original_open(path)

        monkeypatch.setattr(store, "_open_regular_file", boom_open)
        with pytest.raises(HarborStorageError):
            await store.get_bytes("bkt", "k", byte_range=(0, 1), context=context)


@pytest.mark.asyncio
async def test_iter_bytes_open_os_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        original_open = store._open_regular_file
        calls = {"count": 0}

        def boom_open(path: Path) -> object:
            calls["count"] += 1
            if calls["count"] > 1:
                raise OSError("boom")
            return original_open(path)

        monkeypatch.setattr(store, "_open_regular_file", boom_open)
        with pytest.raises(HarborStorageError):
            async for _ in store.iter_bytes("bkt", "k", chunk_size=4, context=context):
                pass


@pytest.mark.asyncio
async def test_iter_bytes_disappearance_is_mapped_to_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        original_open = store._open_regular_file
        calls = {"count": 0}

        def disappear(path: Path) -> object:
            calls["count"] += 1
            if calls["count"] > 1:
                raise FileNotFoundError
            return original_open(path)

        monkeypatch.setattr(store, "_open_regular_file", disappear)
        with pytest.raises(HarborStorageNotFoundError):
            async for _ in store.iter_bytes("bkt", "k", chunk_size=4, context=context):
                pass


@pytest.mark.asyncio
async def test_head_read_text_os_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        original_read = store._read_regular_file
        calls = {"n": 0}

        def flaky_read(path: Path) -> bytes:
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("boom")
            return original_read(path)

        monkeypatch.setattr(store, "_read_regular_file", flaky_read)
        with pytest.raises(HarborStorageError):
            await store.head("bkt", "k", context=context)


@pytest.mark.asyncio
async def test_head_disappearance_is_mapped_to_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        original_read = store._read_regular_file
        calls = {"n": 0}

        def flaky_read(path: Path) -> bytes:
            calls["n"] += 1
            if calls["n"] > 1:
                raise FileNotFoundError
            return original_read(path)

        monkeypatch.setattr(store, "_read_regular_file", flaky_read)
        with pytest.raises(HarborStorageNotFoundError):
            await store.head("bkt", "k", context=context)


@pytest.mark.asyncio
async def test_delete_unlink_os_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        def boom_unlink(_self: Path, missing_ok: bool = False) -> None:
            del missing_ok
            raise OSError("boom")

        monkeypatch.setattr(Path, "unlink", boom_unlink)
        with pytest.raises(HarborStorageError):
            await store.delete("bkt", "k", context=context)


@pytest.mark.asyncio
async def test_list_wraps_corrupt_metadata_as_io_error(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        target = store._safe_path("bkt", "k", context)
        store._meta_path(target).write_text("not valid json", encoding="utf-8")

        with pytest.raises(HarborStorageError):
            await store.list("bkt", "", limit=10, context=context)


@pytest.mark.asyncio
async def test_authorized_path_wraps_corrupt_metadata_as_not_found(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        target = store._safe_path("bkt", "k", context)
        store._meta_path(target).write_text("not valid json", encoding="utf-8")

        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=context)


@pytest.mark.asyncio
async def test_list_skips_object_with_mismatched_tenant_metadata(
    tmp_path: Path,
) -> None:
    """Defensive check: even if a meta file inside a tenant's own directory
    tree somehow carried a different tenant_id, list() must still exclude
    it rather than trust the directory partitioning alone."""
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context("tenant-a")
        reference = await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context
        )
        target = store._safe_path("bkt", "k", context)
        tampered = ObjectMetadata(
            reference=reference,
            metadata={"tenant_id": "someone-else"},
        )
        store._meta_path(target).write_text(tampered.model_dump_json(), encoding="utf-8")

        results = await store.list("bkt", "", limit=10, context=context)
        assert results == []


@pytest.mark.asyncio
async def test_authorized_path_rejects_mismatched_tenant_metadata(
    tmp_path: Path,
) -> None:
    """Defensive check mirroring the list() case above, for direct reads."""
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context("tenant-a")
        reference = await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context
        )
        target = store._safe_path("bkt", "k", context)
        tampered = ObjectMetadata(
            reference=reference,
            metadata={"tenant_id": "someone-else"},
        )
        store._meta_path(target).write_text(tampered.model_dump_json(), encoding="utf-8")

        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=context)
