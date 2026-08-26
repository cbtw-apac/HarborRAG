"""Source-owned canonical chunk validation hooks."""

from __future__ import annotations

from harborrag_core.chunking import ChunkKind, ChunkRecord


def validate_jira_chunk(record: ChunkRecord, errors: list[str], label: str) -> None:
    """Validate metadata required by Jira retrieval projections."""

    issue_key = record.metadata.get("issue_key")
    if not isinstance(issue_key, str) or not issue_key.strip():
        errors.append(f"{label} Jira chunk requires issue_key")
    if record.chunk_kind == ChunkKind.COMMENT:
        comment_id = record.metadata.get("comment_id")
        if not isinstance(comment_id, (str, int)) or not str(comment_id).strip():
            errors.append(f"{label} Jira comment requires comment_id")


def validate_confluence_chunk(
    record: ChunkRecord,
    errors: list[str],
    label: str,
) -> None:
    """Validate metadata required by Confluence retrieval projections."""

    page_id = record.metadata.get("page_id")
    if not isinstance(page_id, (str, int)) or not str(page_id).strip():
        errors.append(f"{label} Confluence chunk requires page_id")
