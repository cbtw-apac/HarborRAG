"""Bounded, redacted console previews of documents and parsed output."""

from __future__ import annotations

import os
import reprlib

from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.security.redaction import redact_secrets

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


def print_parsed(provider: str, parsed: ParsedDocument, *, source: str) -> None:
    """Print a parsed local document the same shape as `print_document`."""
    verbose = _verbose_previews_enabled()
    print(f"\n[{provider}] parsed document")
    print(f"[{provider}] source={source!r}")
    print(f"[{provider}] parser={parsed.parser_name!r}")
    print(f"[{provider}] chars={len(parsed.content)}")
    print(
        f"[{provider}] elements={len(parsed.elements or [])} warnings={len(parsed.warnings or [])}"
    )
    if verbose:
        print(f"[{provider}] content preview={_preview(parsed.content)!r}")
        if parsed.metadata:
            print(f"[{provider}] metadata preview={_preview(parsed.metadata)!r}")


def attachments_passed(provider: str, document) -> bool:
    """Require every attempted attachment to avoid an unsupported/failed state."""
    attachments = (document.metadata or {}).get("attachments") or []
    failures = [
        attachment
        for attachment in attachments
        if attachment.get("status") in {"failed", "unsupported"}
    ]
    if not failures:
        return True
    print(f"[{provider}] attachment smoke failed: {len(failures)} attachment(s) failed")
    return False


def print_failure(provider: str, exc: Exception) -> None:
    """Print a bounded, redacted smoke failure instead of a traceback."""
    detail = redact_secrets(str(exc)).replace("\r", " ").replace("\n", " ")[:500]
    print(f"[{provider}] failed: {type(exc).__name__}: {detail}")
