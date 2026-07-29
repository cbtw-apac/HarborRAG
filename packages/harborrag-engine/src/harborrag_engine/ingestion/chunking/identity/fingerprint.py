from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from hashlib import sha256
from re import compile as compile_pattern
from unicodedata import normalize

from ..errors import ChunkIdentityError

_HORIZONTAL_WHITESPACE = compile_pattern(r"[^\S\n]+")


def normalize_identity_text(value: str) -> str:
    """Normalize Unicode, line endings, and cosmetic whitespace for identity.

    The policy uses Unicode NFC, LF line endings, collapsed horizontal
    whitespace, no surrounding line whitespace, and at most one blank line.
    Case and nonblank line boundaries remain significant.
    """

    normalized = normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_HORIZONTAL_WHITESPACE.sub(" ", line).strip() for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    compacted: list[str] = []
    for line in lines:
        if line or not compacted or compacted[-1]:
            compacted.append(line)
    return "\n".join(compacted)


def canonical_identity_payload(value: object) -> str:
    """Serialize identity input with normalized strings and sorted mapping keys."""

    normalized = _normalize_value(value)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ChunkIdentityError("identity input must be finite JSON data") from exc


def encoded_identifier(kind: str, value: object) -> str:
    """Encode one domain-separated identifier as lowercase SHA-256 hex."""

    if not kind.strip():
        raise ChunkIdentityError("identifier kind must be non-empty")
    payload = canonical_identity_payload({"kind": kind, "value": value})
    return f"{kind}:{sha256(payload.encode('utf-8')).hexdigest()}"


def _normalize_value(value: object) -> object:
    if isinstance(value, str):
        return normalize_identity_text(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ChunkIdentityError(f"identity input type is not supported: {type(value).__name__}")


def content_fingerprint(content: str) -> str:
    """Return the SHA-256 fingerprint for normalized evidence content."""

    normalized = normalize_identity_text(content)
    return sha256(normalized.encode("utf-8")).hexdigest()


def manifest_fingerprint(chunk_ids: Iterable[str]) -> str:
    """Return an order-sensitive fingerprint for chunk revision identities."""

    return encoded_identifier("chunk-manifest", tuple(chunk_ids))
