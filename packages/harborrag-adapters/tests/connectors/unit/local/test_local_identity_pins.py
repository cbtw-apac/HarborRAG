"""Local output keeps its document identity and change-detection values."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors.local import LocalFileConnector
from harborrag_adapters.connectors.local import mappers as local_mappers
from harborrag_core.chunking import ConnectorType

pytestmark = pytest.mark.unit

PINS: dict[str, tuple[str, str, str, str]] = {
    "README.md": (
        "document:4d6c2849b587515d3764c8c75d78596926a154c43bd7469e2978683b6ec5b774",
        "admission-change:2bf87aa209bbb6d70f83f4f265b3ec1a10deca110a3c008b904cbe0d8d2a1159",
        "43bc793c5a02c4136257860080aa8e57e677e099a9b455724db0eeaa93c692b3",
        "retrieval-metadata:741f8aa48608c9e261efb5d1a78ce7930e0995e4e3bd89a224eb027d212567a0",
    ),
    "docs/guide.md": (
        "document:fe5206e766b8365431a8949ea02e2aeaf60bf8d10ab860885ac251f15e1135f2",
        "admission-change:96d16098a387b2ce1900f9d5275111a6d71d32b20ca92fe4d33c64656437aca3",
        "85974831aca8e80b9191667d710f24edb54bd0b9926092ab7f4246407e8c4208",
        "retrieval-metadata:f75917db723bab4903d1c0484c6e63468d60fb84c94ed5d88755cec4eef729f5",
    ),
}

FILES = {
    "README.md": "# Harbor\n\nSee [the guide](docs/guide.md).\n",
    "docs/guide.md": "# Guide\n\nRequest access first.\n",
    "src/app.py": '"""Berth planner."""\n\n\ndef plan(ships):\n    return sorted(ships)\n',
}
# The default version signal is size and mtime, so the pin fixes the mtime.
MTIME_NS = 1_716_584_276_000_000_000
CREATED_AT = 1_716_500_000.0


def test_local_files_keep_their_identity_and_hashes(
    pin_connector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin all four values; the creation time is fixed because on Linux it is the ctime."""
    monkeypatch.setattr(local_mappers, "file_created_timestamp", lambda _stat: CREATED_AT)
    for relative, body in FILES.items():
        path = write_file(tmp_path / relative, body)
        os.utime(path, ns=(MTIME_NS, MTIME_NS))
    connector = LocalFileConnector(config(tmp_path))

    assert pin_connector(connector, connector_type=ConnectorType.LOCAL) == PINS
