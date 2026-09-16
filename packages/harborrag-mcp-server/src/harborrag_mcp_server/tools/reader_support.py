"""Serialization and cursor helpers shared by MCP reader tools."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.ingestion import GraphNodeRecord, ReadableSource
from harborrag_runtime.sdk import EvidenceReadItem, EvidenceReadSelector

from .reader_catalog import CONTRACT_REVISION
from .retrieval_inputs import optional_text, text


def evidence_selector(raw: object) -> EvidenceReadSelector:
    if not isinstance(raw, dict):
        raise HarborValidationError("each evidence item must be an object")
    return EvidenceReadSelector(
        text(raw, "chunk_id"),
        optional_text(raw, "expected_document_id"),
        optional_text(raw, "expected_document_version_id"),
    )


def evidence_item(item: EvidenceReadItem) -> dict[str, object]:
    return {
        "chunk_id": item.chunk_id,
        "availability": item.availability,
        "text": item.text,
        "document_id": item.document_id,
        "document_version_id": item.document_version_id,
        "document_title": item.document_title,
        "source_scope_id": item.source_scope_id,
        "connector_type": item.connector_type,
        "chunk_kind": item.chunk_kind,
        "ordinal": item.ordinal,
        "section_path": list(item.section_path),
        "citation_locator": item.citation_locator,
    }


def source(item: ReadableSource) -> dict[str, object]:
    return {
        "source_id": item.source_scope_id,
        "name": item.display_name,
        "connector_type": item.connector_type,
        "ingestion_state": item.ingestion_state,
        "last_source_check_at": _time(item.last_source_check_at),
        "last_successful_source_check_at": _time(item.last_successful_source_check_at),
        "last_successful_ingestion_at": _time(item.last_successful_ingestion_at),
        "active_document_count": item.active_document_count,
    }


def node(item: GraphNodeRecord) -> dict[str, object]:
    availability = "unknown"
    if item.node_kind.value == "chunk":
        availability = "evidence"
    elif item.document_id is not None:
        availability = "document_context"
    return {
        "node_key": item.node_key,
        "title": item.title,
        "node_kind": item.node_kind.value,
        "entity_type": item.entity_type.value,
        "source_id": item.source_scope_id,
        "content_availability": availability,
    }


def cursor_payload(value: str | None, tool: str) -> dict[str, Any]:
    if value is None:
        raise HarborValidationError("cursor is unavailable")
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise HarborValidationError("cursor is unavailable") from None
    if not isinstance(payload, dict) or payload.get("tool") != tool:
        raise HarborValidationError("cursor is unavailable")
    return payload


def match_cursor_value(
    arguments: dict[str, object], stored: dict[str, Any], name: str, value: object
) -> None:
    if name in arguments and name in stored and stored[name] != value:
        raise HarborValidationError("cursor does not match the requested scope")


def success(
    request_id: str,
    data: dict[str, object],
    *,
    complete: bool,
    reasons: list[str],
) -> dict[str, object]:
    return {
        "ok": True,
        "request_id": request_id,
        "contract_revision": CONTRACT_REVISION,
        **data,
        "completion": {"complete": complete, "reasons": reasons},
    }


def failure(message: str) -> dict[str, object]:
    return {"ok": False, "error": message}


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "cursor_payload",
    "evidence_item",
    "evidence_selector",
    "failure",
    "match_cursor_value",
    "node",
    "source",
    "success",
]
