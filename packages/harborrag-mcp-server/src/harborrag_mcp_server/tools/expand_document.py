"""Tenant-scoped full-document expansion tool."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_runtime.contracts import ExpandDocumentRequest

from .base import BaseMcpTool, McpToolSpec
from .retrieval_inputs import TENANT_PROPERTY, access, text

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG

logger = logging.getLogger("harborrag.mcp.tools.expand_document")


@dataclass(slots=True)
class ExpandDocumentTool(BaseMcpTool):
    """Retrieve the full source document for a document_id returned by a matched chunk."""

    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "expand_document",
        "Retrieve the full source document behind a document_id returned by a matched chunk.",
        {
            "type": "object",
            "required": ["document_id", "tenant_id"],
            "properties": {
                "document_id": {"type": "string", "minLength": 1},
                "tenant_id": TENANT_PROPERTY,
            },
            "additionalProperties": False,
        },
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        try:
            request = ExpandDocumentRequest(
                access=access(arguments, principal_id),
                document_id=text(arguments, "document_id"),
            )
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if self.runtime is None:
            return {"ok": False, "error": "document store backend is not configured"}
        try:
            response = await self.runtime.retrieval.expand_document(request)
        except HarborNotFoundError:
            return {"ok": False, "error": "document not found"}
        except HarborCapabilityError:
            return {"ok": False, "error": "document store backend is not configured"}
        except Exception:
            # The caller only ever sees the generic message below; the real cause is
            # only visible in the server logs, never in the tool response.
            logger.exception("document store backend raised during expand_document")
            return {"ok": False, "error": "document store backend failed"}
        return {"ok": True, **asdict(response)}
