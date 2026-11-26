from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits"),
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.asyncio
@pytest.mark.usefixtures("permissive_umask")
async def test_connect_creates_missing_directories_above_the_root_owner_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harborrag" / "objects"

    await FilesystemObjectStore(root=root).connect()

    assert _mode(tmp_path / ".harborrag") == 0o700
    assert _mode(root) == 0o700


@pytest.mark.asyncio
@pytest.mark.usefixtures("permissive_umask")
async def test_connect_keeps_the_mode_of_an_existing_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)

    await FilesystemObjectStore(root=shared / "objects").connect()

    assert _mode(shared) == 0o755
    assert _mode(shared / "objects") == 0o700
