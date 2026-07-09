"""Minimal shared setup for standalone smoke scripts."""
from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-core" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = _unquote(value)


def env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value else None


def env_path(name: str) -> Path | None:
    value = env(name)
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def print_document(provider: str, document) -> None:
    print(f"\n[{provider}] loaded document")
    print(document)
    print(
        f"[{provider}] id={document.id!r} "
        f"content_type={document.content_type!r} chars={len(document.text())}"
    )
