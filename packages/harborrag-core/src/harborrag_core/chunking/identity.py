from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from re import compile as compile_pattern
from unicodedata import normalize

from harborrag_core.chunking.errors import ChunkContractError, ChunkIdentityError

from .schemas import ChunkKind

_HORIZONTAL_WHITESPACE = compile_pattern(r"[^\S\n]+")


def normalize_identity_text(value: str) -> str:
    """Normalize text using the stable Work 1 identity policy."""

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


def normalize_structural_path(path: Sequence[str]) -> tuple[str, ...]:
    """Normalize an ordered path without changing its case or ancestry."""

    normalized = tuple(" ".join(normalize("NFC", part).split()) for part in path)
    if any(not part for part in normalized):
        raise ChunkContractError("structural path parts must be non-empty")
    return normalized


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


def content_fingerprint(content: str) -> str:
    """Return the SHA-256 fingerprint for normalized evidence content."""

    normalized = normalize_identity_text(content)
    return sha256(normalized.encode("utf-8")).hexdigest()


def manifest_fingerprint(chunk_ids: Iterable[str]) -> str:
    """Return an order-sensitive fingerprint for chunk revision identities."""

    return encoded_identifier("chunk-manifest", tuple(chunk_ids))


class CanonicalIdentityBuilder:
    """Build deterministic source-independent canonical identities."""

    def section_id(self, *, document_id: str, section_path: Sequence[str]) -> str:
        return encoded_identifier(
            "section",
            {
                "document_id": document_id,
                "section_path": normalize_structural_path(section_path),
            },
        )

    def logical_chunk_id(
        self,
        *,
        section_id: str,
        stable_source_range: Mapping[str, object],
        chunk_kind: ChunkKind,
    ) -> str:
        return encoded_identifier(
            "logical-chunk",
            {
                "section_id": section_id,
                "stable_source_range": dict(stable_source_range),
                "chunk_kind": chunk_kind.value,
            },
        )

    def chunk_id(
        self,
        *,
        logical_chunk_id: str,
        document_version_id: str,
        strategy_version: str,
        content_hash: str,
    ) -> str:
        return encoded_identifier(
            "chunk",
            {
                "logical_chunk_id": logical_chunk_id,
                "document_version_id": document_version_id,
                "strategy_version": strategy_version,
                "content_hash": content_hash,
            },
        )

    def table_id(
        self,
        *,
        document_id: str,
        section_path: Sequence[str],
        stable_table_location: Mapping[str, object],
    ) -> str:
        return encoded_identifier(
            "table",
            {
                "document_id": document_id,
                "section_path": normalize_structural_path(section_path),
                "stable_table_location": dict(stable_table_location),
            },
        )

    def table_version_id(
        self,
        *,
        table_id: str,
        source_version: str,
        content_hash: str,
    ) -> str:
        return encoded_identifier(
            "table-version",
            {
                "table_id": table_id,
                "source_version": source_version,
                "content_hash": content_hash,
            },
        )

    def permission_set_id(
        self,
        *,
        tenant_id: str,
        permissions: Mapping[str, object],
    ) -> str:
        return encoded_identifier(
            "permission-set",
            {"tenant_id": tenant_id, "permissions": dict(permissions)},
        )


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
