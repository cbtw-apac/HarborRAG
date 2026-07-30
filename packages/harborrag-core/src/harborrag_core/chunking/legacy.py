from __future__ import annotations

from collections.abc import Mapping
from re import compile
from typing import Any

from .errors import ChunkValidationError
from .schemas import ChunkContext, ChunkKind, ConnectorType, DocumentKind
from .source_schemas import SourceLocator
from .table_schemas import TableChunkLocator

_ROLE_TOKEN_PATTERN = compile(r"[._-]+")
_CONNECTOR_RULES = (
    ("confluence", ConnectorType.CONFLUENCE),
    ("jira", ConnectorType.JIRA),
)
_CHUNK_KIND_RULES = (
    (frozenset({"table"}), ChunkKind.TABLE),
    (frozenset({"code"}), ChunkKind.CODE),
    (frozenset({"comment"}), ChunkKind.COMMENT),
    (frozenset({"event"}), ChunkKind.EVENT),
    (frozenset({"jira", "field"}), ChunkKind.JIRA_FIELD),
)
_DOCUMENT_KIND_BY_CONNECTOR = {
    ConnectorType.CONFLUENCE: DocumentKind.CONFLUENCE_PAGE,
    ConnectorType.JIRA: DocumentKind.JIRA_ISSUE,
    ConnectorType.LOCAL: DocumentKind.LOCAL_FILE,
}


def _role_tokens(role: str) -> frozenset[str]:
    return frozenset(filter(None, _ROLE_TOKEN_PATTERN.split(role.strip().lower())))


def connector_type_for_legacy_source(source_kind: str) -> ConnectorType:
    """Map a former source-kind string to the canonical connector enum."""

    return next(
        (connector_type for marker, connector_type in _CONNECTOR_RULES if marker in source_kind),
        ConnectorType.LOCAL,
    )


def chunk_kind_for_legacy_role(role: str) -> ChunkKind:
    """Map delimited legacy role tokens without incidental substring matches."""

    tokens = _role_tokens(role)
    return next(
        (
            chunk_kind
            for required_tokens, chunk_kind in _CHUNK_KIND_RULES
            if required_tokens <= tokens
        ),
        ChunkKind.EVIDENCE,
    )


def document_kind_for_legacy_role(
    connector: ConnectorType,
    role: str,
) -> DocumentKind:
    """Map a legacy role and connector to the canonical document kind."""

    if "attachment" in _role_tokens(role):
        return DocumentKind.ATTACHMENT
    return _DOCUMENT_KIND_BY_CONNECTOR[connector]


def contextual_prefix_from_legacy_context(context: ChunkContext) -> str:
    """Reproduce deterministic context for records persisted before prefixes."""

    lines: list[str] = []
    if context.title:
        lines.append(f"Document: {context.title}")
    if context.structural_path:
        lines.append(f"Section: {' > '.join(context.structural_path)}")
    return "\n".join(lines)


def metadata_from_legacy_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Copy optional legacy metadata after validating its storage shape."""

    value = payload.get("metadata")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ChunkValidationError("legacy chunk metadata must be a mapping")
    return dict(value)


def source_locator_from_legacy_payload(
    payload: Mapping[str, Any],
) -> SourceLocator | None:
    """Validate the optional former source-span field."""

    value = payload.get("source_span")
    if value is None:
        return None
    return SourceLocator.model_validate(value)


def context_from_legacy_payload(payload: Mapping[str, Any]) -> ChunkContext | None:
    """Validate the optional former retrieval-context field."""

    value = payload.get("context")
    if value is None:
        return None
    return ChunkContext.model_validate(value)


def table_locator_from_legacy_metadata(
    *,
    logical_chunk_id: str,
    chunk_revision_id: str,
    content: str,
    metadata: dict[str, Any],
) -> TableChunkLocator:
    """Build a valid structured locator from former table metadata."""

    lines = content.splitlines()
    column_count = max((len(line.split("\t")) for line in lines), default=1)
    raw_row_start = metadata.get("table_row_start", metadata.get("row_start", 0))
    row_start = _integer_or_default(raw_row_start, default=0)
    default_row_end = row_start + max(len(lines) - 1, 0)
    raw_row_end = metadata.get(
        "table_row_end",
        metadata.get("row_end", default_row_end),
    )
    row_end = _integer_or_default(raw_row_end, default=default_row_end)
    return TableChunkLocator(
        table_id=str(metadata.get("table_id") or f"legacy-table:{logical_chunk_id}"),
        table_version_id=str(
            metadata.get("table_version_id") or f"legacy-table-version:{chunk_revision_id}"
        ),
        row_start=row_start,
        row_end=max(row_start, row_end),
        column_count=column_count,
    )


def _integer_or_default(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default
