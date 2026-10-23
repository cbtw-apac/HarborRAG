"""Document discovery shared by MCP and agent clients."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from harborrag_core.contracts.errors import HarborCapabilityError, HarborValidationError
from harborrag_core.contracts.reader import (
    DOCUMENT_LIST_LIMIT,
    DocumentListRequest,
)
from harborrag_core.contracts.tools import ToolBehavior, ToolInvocationContext

from .base import ToolSpec
from .reader_base import ReaderTool
from .reader_catalog import reader_success_schema
from .reader_support import cursor_payload, failure, success
from .retrieval_inputs import TENANT_PROPERTY, access, integer, optional_text, text

logger = logging.getLogger("harborrag.runtime.tools.documents")

_DOCUMENT = {
    "type": "object",
    "required": [
        "document_id",
        "document_version_id",
        "title",
        "source_scope_id",
        "connector_type",
        "chunk_count",
    ],
    "properties": {
        "document_id": {"type": "string", "minLength": 1},
        "document_version_id": {"type": "string", "minLength": 1},
        "title": {"type": ["string", "null"]},
        "source_scope_id": {"type": "string"},
        "connector_type": {"type": "string"},
        "chunk_count": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}


@dataclass(slots=True)
class ListDocumentsTool(ReaderTool):
    spec = ToolSpec(
        "list_documents",
        "Discover readable active documents without a semantic query. Results are ordered "
        "by document ID; continue with the returned owner-bound cursor. The permission "
        "catalog enforces a 10,000-document enumeration budget.",
        {
            "type": "object",
            "required": ["tenant_id"],
            "properties": {
                "tenant_id": TENANT_PROPERTY,
                "cursor": {"type": "string", "pattern": "^cur_"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": DOCUMENT_LIST_LIMIT,
                    "default": DOCUMENT_LIST_LIMIT,
                },
            },
            "additionalProperties": False,
        },
        output_schema=reader_success_schema(
            {
                "documents": {"type": "array", "items": _DOCUMENT, "maxItems": DOCUMENT_LIST_LIMIT},
                "next_cursor": {"type": ["string", "null"]},
            },
            ["documents", "next_cursor"],
        ),
        behavior=ToolBehavior(read_only=True, idempotent=True),
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
        context: ToolInvocationContext | None = None,
    ) -> dict[str, object]:
        if context is not None and principal_id != context.access.principal_id:
            raise PermissionError("tool principal does not match invocation context")
        try:
            identity = access(arguments, principal_id)
            limit = integer(
                arguments, "limit", DOCUMENT_LIST_LIMIT, minimum=1, maximum=DOCUMENT_LIST_LIMIT
            )
            cursor = optional_text(arguments, "cursor")
            after = None
            if cursor is not None:
                stored = cursor_payload(
                    self.references.resolve(
                        cursor,
                        "cursor",
                        tenant_id=str(identity.tenant_id),
                        principal_id=principal_id,
                    ),
                    "list_documents",
                )
                after = text(stored, "after_document_id")
            response = await self.require_knowledge().list_documents(
                DocumentListRequest(identity, after, limit)
            )
            next_cursor = None
            if response.next_document_id is not None:
                next_cursor = self.references.issue(
                    "cursor",
                    json.dumps(
                        {"tool": "list_documents", "after_document_id": response.next_document_id}
                    ),
                    tenant_id=str(identity.tenant_id),
                    principal_id=principal_id,
                )
            return success(
                response.request_id,
                {
                    "documents": [asdict(item) for item in response.documents],
                    "next_cursor": next_cursor,
                },
                complete=next_cursor is None,
                reasons=[] if next_cursor is None else ["page_limit"],
            )
        except (HarborValidationError, HarborCapabilityError, ValueError) as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("document discovery failed")
            return failure("document discovery failed")
