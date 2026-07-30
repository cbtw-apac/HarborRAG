"""FileSecretsRepository: SecretsPort over a local JSON file (ML2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harborrag_adapters.repositories.secrets.file import FileSecretsRepository


@pytest.mark.asyncio
async def test_put_resolve_delete_round_trip(tmp_path: Path) -> None:
    repo = FileSecretsRepository(tmp_path / "secrets.json")
    ref = await repo.put("hunter2")
    assert ref.startswith("secret://file/")
    assert await repo.resolve(ref) == "hunter2"
    await repo.delete(ref)
    with pytest.raises(KeyError):
        await repo.resolve(ref)


@pytest.mark.asyncio
async def test_resolve_unknown_ref_raises_key_error(tmp_path: Path) -> None:
    repo = FileSecretsRepository(tmp_path / "secrets.json")
    with pytest.raises(KeyError):
        await repo.resolve("secret://file/does-not-exist")


@pytest.mark.asyncio
async def test_delete_unknown_ref_is_a_no_op(tmp_path: Path) -> None:
    repo = FileSecretsRepository(tmp_path / "secrets.json")
    await repo.delete("secret://file/never-existed")


@pytest.mark.asyncio
async def test_puts_are_distinct_and_do_not_clobber_each_other(tmp_path: Path) -> None:
    repo = FileSecretsRepository(tmp_path / "secrets.json")
    first = await repo.put("value-a")
    second = await repo.put("value-b")
    assert first != second
    assert await repo.resolve(first) == "value-a"
    assert await repo.resolve(second) == "value-b"


@pytest.mark.asyncio
async def test_file_never_left_as_a_stray_tmp_write(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    repo = FileSecretsRepository(path)
    await repo.put("hunter2")
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads(path.read_text(encoding="utf-8"))
