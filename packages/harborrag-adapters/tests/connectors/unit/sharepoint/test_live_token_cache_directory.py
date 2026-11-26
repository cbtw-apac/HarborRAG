"""The delegated token cache directory HarborRAG creates under home is owner-only."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from harborrag_adapters.connectors.sharepoint.delegated import (
    DelegatedTokenCache,
    default_token_cache_path,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes"),
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    previous = os.umask(0o022)
    try:
        yield home
    finally:
        os.umask(previous)


def _cache() -> DelegatedTokenCache:
    return DelegatedTokenCache(
        path=default_token_cache_path("demo", "contoso.example", "demo-app"),
        tenant_id="contoso.example",
        client_id="demo-app",
    )


def test_token_cache_save_on_a_fresh_home_creates_every_directory_owner_only(
    home: Path,
) -> None:
    _cache().save("synthetic-refresh-token")

    for directory in (
        home / ".harborrag",
        home / ".harborrag" / "sharepoint",
        home / ".harborrag" / "sharepoint" / "demo",
    ):
        assert _mode(directory) == 0o700, directory


def test_token_cache_save_keeps_the_mode_of_an_existing_directory(home: Path) -> None:
    (home / ".harborrag").mkdir()
    os.chmod(home / ".harborrag", 0o750)

    _cache().save("synthetic-refresh-token")

    assert _mode(home / ".harborrag") == 0o750
    assert _mode(home / ".harborrag" / "sharepoint" / "demo") == 0o700
