"""Shared fixture builders for local-filesystem connector tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harborrag_adapters.connectors import LocalFileConfig


def write_file(path: Path, content: bytes | str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def config(source_path: Path, **overrides: Any) -> LocalFileConfig:
    values = {"source_path": source_path}
    values.update(overrides)
    return LocalFileConfig(**values)
