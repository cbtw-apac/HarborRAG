from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256


def content_fingerprint(content: str) -> str:
    """Return the SHA-256 fingerprint for exact chunk content."""

    return sha256(content.encode("utf-8")).hexdigest()


def manifest_fingerprint(chunk_ids: Iterable[str]) -> str:
    """Return an order-sensitive fingerprint for chunk revision identities."""

    digest = sha256()
    for value in chunk_ids:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
