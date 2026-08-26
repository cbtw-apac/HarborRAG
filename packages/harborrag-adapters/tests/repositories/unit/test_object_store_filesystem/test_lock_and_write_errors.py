from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from harborrag_adapters.repositories.errors import HarborStorageError
from harborrag_adapters.repositories.object_store.filesystem import (
    repository as filesystem_module,
)
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_core.schemas.object_store import PutObjectRequest

from .conftest import make_context


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
