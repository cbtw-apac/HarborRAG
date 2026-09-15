"""Permission-scoped source discovery MCP tool."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from harborrag_core.contracts.errors import HarborCapabilityError, HarborValidationError
from harborrag_runtime.reader_contracts import SOURCE_LIST_LIMIT
from harborrag_runtime.sdk import SourceListRequest, SourceListResponse

from .reader_base import ReaderTool
from .reader_catalog import LIST_SOURCES_SPEC
from .reader_support import cursor_payload, failure, match_cursor_value, source, success
from .retrieval_inputs import access, integer, optional_text, string_list, text

logger = logging.getLogger("harborrag.mcp.tools.sources")


@dataclass(slots=True)
class ListSourcesTool(ReaderTool):
    spec = LIST_SOURCES_SPEC

    async def call(self, arguments: dict[str, object], *, principal_id: str) -> dict[str, object]:
        try:
            tenant_id = text(arguments, "tenant_id")
            source_ids = string_list(arguments, "source_ids")
            connector_types = string_list(arguments, "connector_types")
            stored = self._cursor(arguments, tenant_id, principal_id)
            match_cursor_value(arguments, stored, "source_ids", list(source_ids))
            match_cursor_value(arguments, stored, "connector_types", list(connector_types))
            requested_limit = integer(
                arguments,
                "limit",
                SOURCE_LIST_LIMIT,
                minimum=1,
                maximum=SOURCE_LIST_LIMIT,
            )
            match_cursor_value(arguments, stored, "limit", requested_limit)
            source_ids = tuple(stored.get("source_ids", source_ids))
            connector_types = tuple(stored.get("connector_types", connector_types))
            response = await self.require_runtime().knowledge.list_sources(
                SourceListRequest(
                    access(arguments, principal_id),
                    source_ids,
                    connector_types,
                    stored.get("after_source_id"),
                    int(stored.get("limit", requested_limit)),
                )
            )
            next_cursor = self._next_cursor(
                response, tenant_id, principal_id, source_ids, connector_types
            )
            return success(
                response.request_id,
                {
                    "sources": [source(item) for item in response.sources],
                    "next_cursor": next_cursor,
                },
                complete=next_cursor is None,
                reasons=[] if next_cursor is None else ["page_limit"],
            )
        except (HarborValidationError, ValueError) as exc:
            return failure(str(exc))
        except HarborCapabilityError as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("list_sources failed")
            return failure("source discovery failed")

    def _cursor(
        self, arguments: dict[str, object], tenant_id: str, principal_id: str
    ) -> dict[str, Any]:
        cursor = optional_text(arguments, "cursor")
        if cursor is None:
            return {}
        value = self.reference_store().resolve(
            cursor, "cursor", tenant_id=tenant_id, principal_id=principal_id
        )
        return cursor_payload(value, "list_sources")

    def _next_cursor(
        self,
        response: SourceListResponse,
        tenant_id: str,
        principal_id: str,
        source_ids: tuple[str, ...],
        connector_types: tuple[str, ...],
    ) -> str | None:
        if not response.has_more or not response.sources:
            return None
        payload = json.dumps(
            {
                "tool": "list_sources",
                "source_ids": list(source_ids),
                "connector_types": list(connector_types),
                "after_source_id": response.sources[-1].source_scope_id,
                "limit": len(response.sources),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.reference_store().issue(
            "cursor", payload, tenant_id=tenant_id, principal_id=principal_id
        )


__all__ = ["ListSourcesTool"]
