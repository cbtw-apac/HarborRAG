"""Minimal shared setup for standalone smoke scripts."""

from __future__ import annotations

import os
import reprlib
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

from harborrag_core.security.redaction import redact_secrets  # noqa: E402


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
_VERBOSE_VALUES = {"1", "true", "yes", "on"}


def _verbose_previews_enabled() -> bool:
    """Require an explicit local opt-in before showing provider content."""
    if os.getenv("CI"):
        return False
    return os.getenv("HARBOR_SMOKE_VERBOSE", "").strip().lower() in _VERBOSE_VALUES


def _preview(value: object, *, limit: int = PREVIEW_CHARS) -> str:
    """Render a bounded, redacted preview without formatting whole large values."""
    if isinstance(value, str):
        total = len(value)
        text = value[:limit]
    elif isinstance(value, bytes):
        total = len(value)
        text = repr(value[:limit])
    else:
        renderer = reprlib.Repr()
        renderer.maxstring = limit
        renderer.maxother = limit
        renderer.maxdict = 10
        renderer.maxlist = 10
        text = renderer.repr(value)
        total = len(text)

    text = redact_secrets(text)
    if total <= limit:
        return text
    return f"{text}… (truncated, {total} chars total)"


def print_document(provider: str, document) -> None:
    verbose = _verbose_previews_enabled()
    print(f"\n[{provider}] loaded document")
    print(f"[{provider}] id={document.id!r}")
    print(f"[{provider}] content_type={document.content_type!r}")
    text = document.text()
    print(f"[{provider}] chars={len(text)}")
    if verbose:
        print(f"[{provider}] source preview={_preview(document.source)!r}")
        print(f"[{provider}] content preview={_preview(text)!r}")
        print(f"[{provider}] metadata preview={_preview(document.metadata)!r}")
        if document.raw is not None:
            print(f"[{provider}] raw preview={_preview(document.raw)!r}")
    print_attachments(provider, document, verbose=verbose)


def print_attachments(provider: str, document, *, verbose: bool = False) -> None:
    attachments = (document.metadata or {}).get("attachments") or []
    if not attachments:
        print(f"[{provider}] attachments: none")
        return
    print(f"[{provider}] attachments: {len(attachments)}")
    for index, attachment in enumerate(attachments, start=1):
        title = attachment.get("title")
        status = attachment.get("status")
        size_bytes = attachment.get("size_bytes")
        text = attachment.get("text") or ""
        reason = attachment.get("reason")
        line = f"  - attachment={index} status={status!r} "
        line += f"size_bytes={size_bytes} text_chars={len(text)}"
        if verbose and title:
            line += f" title={_preview(title)!r}"
        if verbose and reason:
            line += f" reason={_preview(reason)!r}"
        print(line)
        if verbose and text:
            print(f"    preview={_preview(text)!r}")
