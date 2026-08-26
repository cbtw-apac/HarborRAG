"""Vector and hybrid retrieval use case behind the stable application envelope."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.sdk import HarborRAG, RetrievalLane, RetrievalRequest

from ..errors import failure_response
from ..schemas import AppResponse
from .presentation import retrieval_response

logger = logging.getLogger("harborrag.app.workflow_control.retrieval")

type RuntimeProvider = Callable[[], HarborRAG]


async def retrieve(  # noqa: PLR0913 - explicit retrieval policy is transport-neutral
    runtime: RuntimeProvider,
    settings: RuntimeSettings,
    query: str,
    *,
    tenant_id: str | None = None,
    principal_id: str = "harborrag-cli",
    top_k: int = 10,
    filters: Mapping[str, object] | None = None,
    lane: RetrievalLane = RetrievalLane.HYBRID,
    observe_graph: bool = False,
    include_content: bool = False,
    include_metadata: bool = False,
    score_threshold: float = 0.0,
) -> AppResponse:
    selected_tenant = tenant_id or settings.ingestion_tenant_id
    try:
        response = await runtime().retrieval.search(
            RetrievalRequest(
                access=AccessContext(
                    principal_id=principal_id,
                    tenant_id=TenantId(selected_tenant),
                ),
                query=query,
                top_k=top_k,
                filters=dict(filters or {}),
                lane=lane,
                observe_graph=observe_graph,
            )
        )
        return retrieval_response(
            response,
            include_content=include_content,
            include_metadata=include_metadata,
            top_k=top_k,
            score_threshold=score_threshold,
        )
    except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
        return failure_response(logger, exc, "retrieve for tenant %r", selected_tenant)


__all__ = ["retrieve"]
