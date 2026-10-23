"""Stable content identity for extracted long-term memories.

Extraction is add-only, so the same conversation replayed after a retry must
not create a second row for a fact already stored. ``content_hash`` is that
idempotency key: a digest of the fact's comparison form together with the
scope it is stored at, so the same sentence stored for a session and for a
user stays two distinct memories.
"""

from __future__ import annotations

from hashlib import sha256

from harborrag_core.ports.memory import MemoryScope


def normalize_content(content: str) -> str:
    """Return ``content`` folded to the form two restatements share.

    Whitespace runs collapse to one space and case is folded, so "Dana  Lee
    owns ingest" and "dana lee owns ingest" carry one identity. Nothing else
    is rewritten: dropping punctuation or stop words would fuse facts that
    genuinely differ.
    """

    return " ".join(content.split()).casefold()


def content_hash(content: str, scope: MemoryScope) -> str:
    """Return the digest identifying ``content`` within ``scope``."""

    return sha256(f"{scope.value}\n{normalize_content(content)}".encode()).hexdigest()


__all__ = ["content_hash", "normalize_content"]
