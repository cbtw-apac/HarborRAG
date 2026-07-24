from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
)
from harborrag_adapters.repositories.object_store.filesystem import (
    repository as filesystem_module,
)
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext(tenant_id=tenant)


@pytest.fixture(autouse=True)
def immediate_filesystem_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid the sandbox's unavailable worker-thread executor in unit tests."""

    async def immediate(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(filesystem_module.asyncio, "to_thread", immediate)


@pytest.mark.asyncio
async def test_put_get_delete_round_trip(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(
            PutObjectRequest(bucket="bkt", key="a/b.txt", body=b"hello"),
            context=context,
        )
        assert await store.get_bytes("bkt", "a/b.txt", byte_range=None, context=context) == b"hello"
        assert await store.delete("bkt", "a/b.txt", context=context) is True


@pytest.mark.asyncio
async def test_object_key_cannot_escape_bucket_root(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError):
            await store.put(
                PutObjectRequest(bucket="bkt", key="../../etc/passwd", body=b"x"),
                context=context,
            )


@pytest.mark.asyncio
async def test_checksum_mismatch_leaves_no_temp_file(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(HarborStorageValidationError):
            await store.put(
                PutObjectRequest(bucket="bkt", key="k", body=b"hello", checksum_sha256="0" * 64),
                context=context,
            )
        bucket_dir = tmp_path / "bkt"
        leftover = list(bucket_dir.rglob("*.tmp")) if bucket_dir.exists() else []
        assert leftover == []


@pytest.mark.asyncio
async def test_cross_tenant_put_does_not_collide_with_existing_object(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        owner = make_context("tenant-a")
        other = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"owned"), context=owner)
        # Object paths are tenant-partitioned, so the same logical bucket/key
        # never collides across tenants; each tenant's write is independent.
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"separate"), context=other)
        assert await store.get_bytes("bkt", "k", byte_range=None, context=owner) == b"owned"
        assert await store.get_bytes("bkt", "k", byte_range=None, context=other) == b"separate"


@pytest.mark.asyncio
async def test_concurrent_if_none_match_put_has_one_winner(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def slow_body():
            first_started.set()
            await release_first.wait()
            yield b"first"

        first = asyncio.create_task(
            store.put(
                PutObjectRequest(bucket="bkt", key="atomic", body=slow_body(), if_none_match=True),
                context=context,
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            store.put(
                PutObjectRequest(bucket="bkt", key="atomic", body=b"second", if_none_match=True),
                context=context,
            )
        )
        release_first.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert sum(not isinstance(item, Exception) for item in results) == 1
        assert sum(isinstance(item, HarborStorageAlreadyExistsError) for item in results) == 1


@pytest.mark.asyncio
async def test_read_disappearance_is_mapped_to_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context)

        def disappear(_path: Path) -> bytes:
            raise FileNotFoundError

        monkeypatch.setattr(Path, "read_bytes", disappear)
        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=context)


# --- Additional coverage below: root validation, health, byte-range reads,
# iter_bytes/head/exists/list, lock timeout, IO-error mapping, and cross-tenant
# read isolation. ---


def test_missing_root_and_config_raises_value_error() -> None:
    with pytest.raises(ValueError, match="root is required"):
        FilesystemObjectStore()


@pytest.mark.asyncio
async def test_health_reflects_connection_state(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)

    before = await store.health()
    assert before.status == HealthStatus.UNHEALTHY

    async with store:
        after = await store.health()
        assert after.status == HealthStatus.HEALTHY
        assert after.details["writable"] is True


@pytest.mark.asyncio
async def test_get_bytes_with_valid_byte_range(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"abcdefghij"), context=context
        )
        data = await store.get_bytes("bkt", "k", byte_range=(2, 5), context=context)
        assert data == b"cdef"


@pytest.mark.asyncio
async def test_get_bytes_with_invalid_byte_range_raises_validation_error(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)
        with pytest.raises(HarborStorageValidationError):
            await store.get_bytes("bkt", "k", byte_range=(5, 1), context=context)
        with pytest.raises(HarborStorageValidationError):
            await store.get_bytes("bkt", "k", byte_range=(-1, 2), context=context)


@pytest.mark.asyncio
async def test_iter_bytes_streams_in_chunks(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"abcdefghij"), context=context
        )
        chunks = [
            chunk async for chunk in store.iter_bytes("bkt", "k", chunk_size=4, context=context)
        ]
        assert chunks == [b"abcd", b"efgh", b"ij"]


@pytest.mark.asyncio
async def test_iter_bytes_missing_key_raises_not_found(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(HarborStorageNotFoundError):
            async for _ in store.iter_bytes("bkt", "missing", chunk_size=4, context=context):
                pass


@pytest.mark.asyncio
async def test_head_returns_metadata(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        reference = await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context
        )
        metadata = await store.head("bkt", "k", context=context)
        assert metadata.reference.checksum_sha256 == reference.checksum_sha256


@pytest.mark.asyncio
async def test_exists_true_and_false(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        assert await store.exists("bkt", "k", context=context) is False
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context)
        assert await store.exists("bkt", "k", context=context) is True


@pytest.mark.asyncio
async def test_cross_tenant_get_is_not_found(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        owner = make_context("tenant-a")
        intruder = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"secret"), context=owner)
        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=intruder)


@pytest.mark.asyncio
async def test_delete_returns_false_when_bucket_directory_missing(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        assert await store.delete("bkt", "k", context=context) is False


@pytest.mark.asyncio
async def test_delete_returns_false_when_key_missing_but_bucket_exists(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k1", body=b"data"), context=context)
        assert await store.delete("bkt", "k2", context=context) is False


@pytest.mark.asyncio
async def test_list_respects_prefix_tenant_and_limit(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context("tenant-a")
        other = make_context("tenant-b")
        await store.put(PutObjectRequest(bucket="bkt", key="docs/a", body=b"1"), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="docs/b", body=b"2"), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="other/c", body=b"3"), context=context)
        await store.put(PutObjectRequest(bucket="bkt", key="docs/d", body=b"4"), context=other)

        results = await store.list("bkt", "docs/", limit=10, context=context)
        assert {item.reference.key for item in results} == {"docs/a", "docs/b"}

        limited = await store.list("bkt", "docs/", limit=1, context=context)
        assert len(limited) == 1


@pytest.mark.asyncio
async def test_list_returns_empty_when_bucket_root_missing(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        assert await store.list("bkt", "", limit=10, context=context) == []


@pytest.mark.asyncio
async def test_list_with_invalid_bucket_name_raises_value_error(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError, match="invalid filesystem bucket name"):
            await store.list("bad bucket!", "", limit=10, context=context)


@pytest.mark.asyncio
async def test_invalid_bucket_name_on_put_raises_value_error(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError, match="invalid filesystem bucket name"):
            await store.put(
                PutObjectRequest(bucket="bad bucket!", key="k", body=b"x"),
                context=context,
            )


@pytest.mark.asyncio
async def test_key_with_empty_segment_is_rejected(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(ValueError, match="invalid object key"):
            await store.put(PutObjectRequest(bucket="bkt", key="a//b", body=b"x"), context=context)


@pytest.mark.asyncio
async def test_presign_download_is_not_implemented(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        with pytest.raises(NotImplementedError):
            await store.presign_download("bkt", "k", expires_seconds=60, context=context)


@pytest.mark.asyncio
async def test_put_mkdir_os_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()

        def boom_mkdir(*args: Any, **kwargs: Any) -> None:
            raise OSError("boom")

        monkeypatch.setattr(Path, "mkdir", boom_mkdir)
        with pytest.raises(HarborStorageError):
            await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"x"), context=context)


@pytest.mark.asyncio
async def test_put_over_corrupt_existing_metadata_is_wrapped(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        target = store._safe_path("bkt", "k", context)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")
        store._meta_path(target).write_text("not valid json", encoding="utf-8")

        with pytest.raises(HarborStorageError):
            await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"new"), context=context)


@pytest.mark.asyncio
async def test_lock_acquire_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()

        def always_exists(*args: Any, **kwargs: Any) -> int:
            raise FileExistsError

        monkeypatch.setattr(filesystem_module.os, "open", always_exists)

        loop = asyncio.get_running_loop()
        real_time = loop.time
        calls = {"n": 0}

        def fake_time() -> float:
            calls["n"] += 1
            return real_time() + (1000.0 if calls["n"] > 1 else 0.0)

        monkeypatch.setattr(loop, "time", fake_time)

        with pytest.raises(HarborStorageError, match="timed out"):
            await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"x"), context=context)


@pytest.mark.asyncio
async def test_release_lock_swallows_close_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()

        def boom_close(_fd: int) -> None:
            raise OSError("boom")

        monkeypatch.setattr(filesystem_module.os, "close", boom_close)

        # The put still succeeds even though releasing the lock's file
        # descriptor fails internally; the error must be swallowed.
        reference = await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context
        )
        assert reference.size_bytes == 4


@pytest.mark.asyncio
async def test_cleanup_path_swallows_unlink_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()

        def boom_unlink(_self: Path, missing_ok: bool = False) -> None:
            del missing_ok
            raise OSError("boom")

        monkeypatch.setattr(Path, "unlink", boom_unlink)

        # Cleanup of temp/lock files fails internally but must not surface;
        # the put should still succeed.
        reference = await store.put(
            PutObjectRequest(bucket="bkt", key="k", body=b"data"), context=context
        )
        assert reference.size_bytes == 4


@pytest.mark.asyncio
async def test_get_bytes_range_os_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"abcdef"), context=context)

        original_open = Path.open

        def boom_open(self: Path, *args: object, **kwargs: object) -> object:
            mode = args[0] if args else kwargs.get("mode", "r")
            if mode == "rb":
                raise OSError("boom")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", boom_open)
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

        original_open = Path.open

        def boom_open(self: Path, *args: object, **kwargs: object) -> object:
            mode = args[0] if args else kwargs.get("mode", "r")
            if mode == "rb":
                raise OSError("boom")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", boom_open)
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

        original_open = Path.open

        def disappear(self: Path, *args: object, **kwargs: object) -> object:
            mode = args[0] if args else kwargs.get("mode", "r")
            if mode == "rb":
                raise FileNotFoundError
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", disappear)
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

        original_read_text = Path.read_text
        calls = {"n": 0}

        def flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("boom")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky_read_text)
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

        original_read_text = Path.read_text
        calls = {"n": 0}

        def flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] > 1:
                raise FileNotFoundError
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky_read_text)
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
        from harborrag_core.schemas.object_store import ObjectMetadata

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
        from harborrag_core.schemas.object_store import ObjectMetadata

        tampered = ObjectMetadata(
            reference=reference,
            metadata={"tenant_id": "someone-else"},
        )
        store._meta_path(target).write_text(tampered.model_dump_json(), encoding="utf-8")

        with pytest.raises(HarborStorageNotFoundError):
            await store.get_bytes("bkt", "k", byte_range=None, context=context)


@pytest.mark.asyncio
async def test_lock_acquire_permission_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    async with store:
        context = make_context()

        def boom_open(*args: object, **kwargs: object) -> int:
            raise PermissionError("boom")

        monkeypatch.setattr(filesystem_module.os, "open", boom_open)
        with pytest.raises(HarborStorageError):
            await store.put(PutObjectRequest(bucket="bkt", key="k", body=b"x"), context=context)
