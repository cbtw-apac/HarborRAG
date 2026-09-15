"""Reader MCP tools backed by publication-safe runtime use cases."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from harborrag_core.contracts.errors import HarborCapabilityError, HarborValidationError
from harborrag_core.retrieval import GraphNodeResolutionQuery, GraphNodeSelectorKind
from harborrag_runtime.sdk import (
    DocumentContextRequest,
    DocumentContextResponse,
    EvidenceReadRequest,
    GraphNodeResolveRequest,
)

from .reader_base import ReaderTool
from .reader_catalog import (
    FETCH_EVIDENCE_SPEC,
    GET_DOCUMENT_CONTEXT_SPEC,
    RESOLVE_GRAPH_NODES_SPEC,
)
from .reader_support import (
    cursor_payload,
    evidence_item,
    evidence_selector,
    failure,
    match_cursor_value,
    node,
    success,
)
from .retrieval_inputs import access, boolean, integer, optional_text, string_list, text
from .source_list_tool import ListSourcesTool

logger = logging.getLogger("harborrag.mcp.tools.readers")


@dataclass(slots=True)
class FetchEvidenceTool(ReaderTool):
    spec = FETCH_EVIDENCE_SPEC

    async def call(
        self, arguments: dict[str, object], *, principal_id: str
    ) -> dict[str, object]:
        try:
            raw_items = arguments.get("items")
            if not isinstance(raw_items, list):
                raise HarborValidationError("items must be an array")
            selectors = tuple(evidence_selector(item) for item in raw_items)
            response = await self.require_runtime().knowledge.read_evidence(
                EvidenceReadRequest(access(arguments, principal_id), selectors)
            )
            items = [evidence_item(item) for item in response.items]
            reasons = sorted(
                {
                    str(item["availability"])
                    for item in items
                    if item["availability"] != "available"
                }
            )
            return success(
                response.request_id,
                {"items": items},
                complete=not reasons,
                reasons=reasons,
            )
        except (HarborValidationError, ValueError) as exc:
            return failure(str(exc))
        except HarborCapabilityError as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("fetch_evidence failed")
            return failure("evidence retrieval failed")


@dataclass(slots=True)
class GetDocumentContextTool(ReaderTool):
    spec = GET_DOCUMENT_CONTEXT_SPEC

    async def call(
        self, arguments: dict[str, object], *, principal_id: str
    ) -> dict[str, object]:
        try:
            selected = self._context_arguments(arguments, principal_id)
            response = await self.require_runtime().knowledge.get_document_context(
                DocumentContextRequest(
                    access=access(arguments, principal_id),
                    document_id=selected["document_id"],
                    expected_document_version_id=selected["version_id"],
                    anchor_chunk_id=selected["anchor_chunk_id"],
                    anchor_section_path=selected["anchor_section_path"],
                    offset=selected["offset"],
                    limit=selected["limit"],
                    include_outline=selected["include_outline"],
                )
            )
            next_cursor = self._context_cursor(response, selected, arguments, principal_id)
            outline_complete = bool(
                selected["include_outline"]
                and selected["offset"] == 0
                and not selected["anchor_chunk_id"]
                and not selected["anchor_section_path"]
                and next_cursor is None
                and response.outcome == "ok"
            )
            reasons = []
            if response.outcome != "ok":
                reasons.append(response.outcome)
            if next_cursor is not None:
                reasons.append("page_limit")
            if selected["include_outline"] and not outline_complete:
                reasons.append("outline_window_only")
            return success(
                response.request_id,
                {
                    "outcome": response.outcome,
                    "document_id": response.document_id,
                    "document_version_id": response.document_version_id,
                    "chunks": [
                        {
                            "chunk_id": item.chunk_id,
                            "ordinal": item.ordinal,
                            "text": item.text,
                            "chunk_kind": item.chunk_kind,
                            "section_path": list(item.section_path),
                            "citation_locator": item.citation_locator,
                        }
                        for item in response.chunks
                    ],
                    "outline": [list(path) for path in response.outline],
                    "outline_complete": outline_complete,
                    "next_cursor": next_cursor,
                },
                complete=not reasons,
                reasons=reasons,
            )
        except (HarborValidationError, ValueError) as exc:
            return failure(str(exc))
        except HarborCapabilityError as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("get_document_context failed")
            return failure("document context retrieval failed")

    def _context_arguments(
        self, arguments: dict[str, object], principal_id: str
    ) -> dict[str, Any]:
        tenant_id = text(arguments, "tenant_id")
        document_id = text(arguments, "document_id")
        cursor = optional_text(arguments, "cursor")
        stored: dict[str, Any] = {}
        if cursor is not None:
            value = self.reference_store().resolve(
                cursor, "cursor", tenant_id=tenant_id, principal_id=principal_id
            )
            stored = cursor_payload(value, "get_document_context")
            if stored.get("document_id") != document_id:
                raise HarborValidationError("cursor is unavailable for this document")
        version = optional_text(arguments, "expected_document_version_id")
        anchor_chunk = optional_text(arguments, "anchor_chunk_id")
        anchor_path = string_list(arguments, "anchor_section_path")
        if version is not None and stored.get("version_id", version) != version:
            raise HarborValidationError("cursor does not match the requested version")
        match_cursor_value(arguments, stored, "anchor_chunk_id", anchor_chunk)
        match_cursor_value(arguments, stored, "anchor_section_path", list(anchor_path))
        requested_limit = integer(arguments, "limit", 10, minimum=1, maximum=10)
        requested_outline = boolean(arguments, "include_outline", False)
        match_cursor_value(arguments, stored, "limit", requested_limit)
        match_cursor_value(arguments, stored, "include_outline", requested_outline)
        return {
            "document_id": document_id,
            "version_id": stored.get("version_id", version),
            "anchor_chunk_id": stored.get("anchor_chunk_id", anchor_chunk),
            "anchor_section_path": tuple(stored.get("anchor_section_path", anchor_path)),
            "offset": int(stored.get("offset", 0)),
            "limit": int(stored.get("limit", requested_limit)),
            "include_outline": bool(stored.get("include_outline", requested_outline)),
        }

    def _context_cursor(
        self,
        response: DocumentContextResponse,
        selected: dict[str, Any],
        arguments: dict[str, object],
        principal_id: str,
    ) -> str | None:
        next_offset = response.next_offset
        version_id = response.document_version_id
        if next_offset is None or version_id is None:
            return None
        payload = json.dumps(
            {
                "tool": "get_document_context",
                "document_id": selected["document_id"],
                "version_id": version_id,
                "anchor_chunk_id": selected["anchor_chunk_id"],
                "anchor_section_path": list(selected["anchor_section_path"]),
                "offset": next_offset,
                "limit": selected["limit"],
                "include_outline": selected["include_outline"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.reference_store().issue(
            "cursor",
            payload,
            tenant_id=text(arguments, "tenant_id"),
            principal_id=principal_id,
        )


@dataclass(slots=True)
class ResolveGraphNodesTool(ReaderTool):
    spec = RESOLVE_GRAPH_NODES_SPEC

    async def call(
        self, arguments: dict[str, object], *, principal_id: str
    ) -> dict[str, object]:
        try:
            selector = arguments.get("selector")
            if not isinstance(selector, dict):
                raise HarborValidationError("selector must be an object")
            query = GraphNodeResolutionQuery(
                selector_kind=GraphNodeSelectorKind(text(selector, "kind")),
                value=text(selector, "value"),
                source_scope_ids=string_list(arguments, "source_ids"),
                entity_types=string_list(arguments, "entity_types"),
                limit=integer(arguments, "limit", 5, minimum=1, maximum=10),
            )
            response = await self.require_runtime().knowledge.resolve_graph_nodes(
                GraphNodeResolveRequest(access(arguments, principal_id), query)
            )
            candidates = [node(item) for item in response.candidates]
            resolution = (
                "no_match" if not candidates else "unique" if len(candidates) == 1 and not response.truncated else "ambiguous"
            )
            return success(
                response.request_id,
                {"resolution": resolution, "candidates": candidates},
                complete=not response.truncated,
                reasons=[] if not response.truncated else ["candidate_limit"],
            )
        except (HarborValidationError, ValueError) as exc:
            return failure(str(exc))
        except HarborCapabilityError as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("resolve_graph_nodes failed")
            return failure("graph node resolution failed")


__all__ = [
    "FetchEvidenceTool",
    "GetDocumentContextTool",
    "ListSourcesTool",
    "ResolveGraphNodesTool",
]
