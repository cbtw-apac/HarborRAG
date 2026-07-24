"""Unit tests for local-filesystem connector configuration validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from local_test_helpers import config

from harborrag_adapters.connectors import LocalFileConfig

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_config_requires_existing_file_or_folder(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        LocalFileConfig(source_path=tmp_path / "missing")

    cfg = config(tmp_path, allowed_extensions={"md", ".PY"}, excluded_extensions={"tmp"})

    assert cfg.source_path == tmp_path.resolve()
    assert cfg.allowed_extensions == {".md", ".py"}
    assert cfg.excluded_extensions == {".tmp"}


def test_config_rejects_special_file_types(tmp_path: Path, monkeypatch):
    fifo_path = tmp_path / "pipe"
    try:
        os.mkfifo(fifo_path)
    except (AttributeError, OSError):
        original_exists = Path.exists
        original_is_file = Path.is_file
        original_is_dir = Path.is_dir
        monkeypatch.setattr(
            Path,
            "exists",
            lambda path: path == fifo_path or original_exists(path),
        )
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda path: False if path == fifo_path else original_is_file(path),
        )
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda path: False if path == fifo_path else original_is_dir(path),
        )

    with pytest.raises(ValueError, match="must be a regular file or directory"):
        LocalFileConfig(source_path=fifo_path)


def test_config_rejects_invalid_checksum_mode(tmp_path: Path):
    with pytest.raises(ValueError, match="checksum_mode must be one of"):
        config(tmp_path, checksum_mode="crc32")
