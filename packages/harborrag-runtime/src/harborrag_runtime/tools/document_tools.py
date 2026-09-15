"""Document discovery and citation verification shared by MCP and agent clients."""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import asdict, dataclass

from harborrag_core.contracts.errors import HarborCapabilityError, HarborValidationError
from harborrag_runtime.reader_contracts import (
    DOCUMENT_LIST_LIMIT,
    DocumentListRequest,
    DocumentMetadataRequest,
    EvidenceReadRequest,
)

from .base import ToolSpec
from .reader_base import ReaderTool
from .reader_catalog import (
    FETCH_EVIDENCE_SPEC,
    READ_ONLY_ANNOTATIONS,
    reader_success_schema,
)
from .reader_support import cursor_payload, evidence_selector, failure, success
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
_VERIFY_INPUT = deepcopy(FETCH_EVIDENCE_SPEC.input_schema)
_VERIFY_INPUT["properties"]["items"]["items"]["properties"]["expected_content_sha256"] = {
    "type": "string",
    "pattern": "^[a-f0-9]{64}$",
}


@dataclass(slots=True)
class GetDocumentMetadataTool(ReaderTool):
    spec = ToolSpec(
        "get_document_metadata",
        "Read current document title, version, source, connector and chunk count without "
        "returning content or storage addresses. Unreadable and unpublished documents "
        "have the same unavailable result.",
        {
            "type": "object",
            "required": ["tenant_id", "document_id"],
            "properties": {
                "tenant_id": TENANT_PROPERTY,
                "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "additionalProperties": False,
        },
        output_schema=reader_success_schema(
            {"document": {"anyOf": [_DOCUMENT, {"type": "null"}]}}, ["document"]
        ),
        annotations=READ_ONLY_ANNOTATIONS,
    )

    async def call(self, arguments: dict[str, object], *, principal_id: str) -> dict[str, object]:
        try:
            response = await self.require_runtime().knowledge.get_document_metadata(
                DocumentMetadataRequest(
                    access(arguments, principal_id), text(arguments, "document_id")
                )
            )
            document = response.document
            return success(
                response.request_id,
                {"document": asdict(document) if document is not None else None},
                complete=document is not None,
                reasons=[] if document is not None else ["unavailable"],
            )
        except (HarborValidationError, HarborCapabilityError, ValueError) as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("document metadata retrieval failed")
            return failure("document metadata retrieval failed")


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
        annotations=READ_ONLY_ANNOTATIONS,
    )

    async def call(self, arguments: dict[str, object], *, principal_id: str) -> dict[str, object]:
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
            response = await self.require_runtime().knowledge.list_documents(
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


@dataclass(slots=True)
class VerifyCitationsTool(ReaderTool):
    spec = ToolSpec(
        "verify_citations",
        "Recheck up to ten citation chunk IDs against current publication, expected "
        "document/version and caller permissions. Returns validity and SHA-256 of "
        "canonical UTF-8 content without returning the content itself.",
        _VERIFY_INPUT,
        output_schema=reader_success_schema(
            {
                "items": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "required": ["chunk_id", "valid", "content_sha256"],
                        "properties": {
                            "chunk_id": {"type": "string"},
                            "valid": {"type": "boolean"},
                            "content_sha256": {"type": ["string", "null"]},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            ["items"],
        ),
        annotations=READ_ONLY_ANNOTATIONS,
    )

    async def call(self, arguments: dict[str, object], *, principal_id: str) -> dict[str, object]:
        try:
            raw = arguments.get("items")
            if not isinstance(raw, list):
                raise HarborValidationError("items must be an array")
            expected_hashes = {
                text(item, "chunk_id"): optional_text(item, "expected_content_sha256")
                for item in raw
                if isinstance(item, dict)
            }
            response = await self.require_runtime().knowledge.read_evidence(
                EvidenceReadRequest(
                    access(arguments, principal_id), tuple(evidence_selector(item) for item in raw)
                )
            )
            items = [
                {
                    "chunk_id": item.chunk_id,
                    "valid": item.availability == "available",
                    "content_sha256": hashlib.sha256(item.text.encode("utf-8")).hexdigest()
                    if item.availability == "available" and item.text is not None
                    else None,
                }
                for item in response.items
            ]
            for item in items:
                expected = expected_hashes.get(str(item["chunk_id"]))
                if expected is not None and item["content_sha256"] != expected:
                    item["valid"] = False
            complete = all(item["valid"] for item in items)
            return success(
                response.request_id,
                {"items": items},
                complete=complete,
                reasons=[] if complete else ["citation_invalid"],
            )
        except (HarborValidationError, HarborCapabilityError, ValueError) as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("citation verification failed")
            return failure("citation verification failed")
