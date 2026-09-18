"""Semantic expansion followed by canonical source evidence reads."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from harborrag_core.contracts.errors import HarborCapabilityError, HarborValidationError
from harborrag_core.contracts.reader import (
    EvidenceReadRequest,
    EvidenceReadSelector,
)
from harborrag_core.contracts.tools import ToolBehavior, ToolInvocationContext

from .base import ToolSpec
from .reader_base import ReaderTool
from .reader_catalog import EVIDENCE_ITEM_SCHEMA, reader_success_schema
from .reader_support import evidence_item, failure, success
from .retrieval_cost import RETRIEVAL_COST_SCHEMA
from .retrieval_inputs import access, integer
from .vector_search import VectorSearchTool

logger = logging.getLogger("harborrag.runtime.tools.composed_search")

_INPUT_SCHEMA = deepcopy(VectorSearchTool.spec.input_schema)
_INPUT_SCHEMA["properties"].pop("mode")
_INPUT_SCHEMA["properties"].pop("include_content")
_INPUT_SCHEMA["properties"]["top_k"]["maximum"] = 10


@dataclass(slots=True)
class ComposedEvidenceSearchTool(ReaderTool):
    spec = ToolSpec(
        "composed_evidence_search",
        "Search evidence with bounded local semantic expansion, then fetch the selected "
        "canonical chunks. Graph associations guide retrieval; only returned source "
        "content supports citations. At most ten evidence chunks are returned.",
        _INPUT_SCHEMA,
        output_schema=reader_success_schema(
            {
                "items": {"type": "array", "items": EVIDENCE_ITEM_SCHEMA, "maxItems": 10},
                "diagnostics": {"type": "object"},
                "cost": RETRIEVAL_COST_SCHEMA,
            },
            ["items", "diagnostics", "cost"],
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
            retrieval = self.require_retrieval()
            knowledge = self.require_knowledge()
            integer(arguments, "top_k", 5, minimum=1, maximum=10)
            search = await VectorSearchTool(reader=retrieval).call(
                {**arguments, "mode": "local_semantic", "include_content": False},
                principal_id=principal_id,
                context=context,
            )
            if search.get("ok") is not True:
                return search
            selectors = tuple(
                EvidenceReadSelector(
                    chunk_id=item["id"],
                    expected_document_id=item["metadata"].get("document_id"),
                    expected_document_version_id=item["metadata"].get("document_version_id"),
                )
                for item in cast("list[dict[str, Any]]", search["results"])
            )
            items = []
            if selectors:
                evidence = await knowledge.read_evidence(
                    EvidenceReadRequest(access(arguments, principal_id), selectors)
                )
                items = [evidence_item(item) for item in evidence.items]
            reasons = sorted(
                {str(item["availability"]) for item in items if item["availability"] != "available"}
            )
            return success(
                str(search["request_id"]),
                {"items": items, "diagnostics": search["diagnostics"], "cost": search["cost"]},
                complete=not reasons,
                reasons=reasons,
            )
        except (HarborValidationError, HarborCapabilityError, ValueError) as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("composed evidence retrieval failed")
            return failure("composed evidence retrieval failed")
