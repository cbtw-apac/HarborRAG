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


PREVIEW_CHARS = 200


def _preview(value: object, *, limit: int = PREVIEW_CHARS) -> str:
    """Render a short, safe preview of a value that may be huge (raw bytes/text)."""
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (truncated, {len(text)} chars total)"


def print_document(provider: str, document) -> None:
    print(f"\n[{provider}] loaded document")
    print(f"[{provider}] id={document.id!r} source={document.source!r}")
    print(f"[{provider}] content_type={document.content_type!r}")
    text = document.text()
    print(f"[{provider}] chars={len(text)} preview={_preview(text)!r}")
    print(f"[{provider}] metadata preview={_preview(document.metadata)!r}")
    if document.raw is not None:
        print(f"[{provider}] raw preview={_preview(document.raw)!r}")
    print_attachments(provider, document)


def print_attachments(provider: str, document) -> None:
    attachments = (document.metadata or {}).get("attachments") or []
    if not attachments:
        print(f"[{provider}] attachments: none")
        return
    print(f"[{provider}] attachments: {len(attachments)}")
    for attachment in attachments:
        title = attachment.get("title")
        status = attachment.get("status")
        size_bytes = attachment.get("size_bytes")
        text = attachment.get("text") or ""
        reason = attachment.get("reason")
        line = (
            f"  - {title!r} status={status!r} size_bytes={size_bytes} "
            f"text_chars={len(text)}"
        )
        if reason:
            line += f" reason={reason!r}"
        print(line)
        if text:
            print(f"    preview={_preview(text)!r}")
