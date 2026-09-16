"""Canonical document ACLs constrain vector ranking, then guard the final read."""

from __future__ import annotations

import asyncio
import logging

from harborrag_core.indexing import (
    FilterOperator,
    VectorFilter,
    VectorFilterCondition,
    VectorSearchResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.search import TopologySearchPort

logger = logging.getLogger("harborrag.runtime.retrieval.permissions")


class RetrievalPermissions:
    def __init__(self, repository: TopologySearchPort | None) -> None:
        self._repository = repository

    async def scope_filter(
        self, filters: VectorFilter | None, context: StorageOperationContext
    ) -> VectorFilter | None:
        if self._repository is None:
            # Explicit legacy embedding-only callers. Production composition
            # always supplies canonical authority; unknown permissions there deny.
            return filters
        try:
            async with asyncio.timeout(2):
                documents = await self._repository.allowed_document_ids(
                    str(context.tenant_id), access=context.access, limit=10000
                )
        except Exception as error:
            logger.warning(
                "Permission scope unavailable", extra={"error_code": type(error).__name__}
            )
            documents = ()
        result = filters if filters is not None else VectorFilter()
        return VectorFilter(
            must=[
                *result.must,
                VectorFilterCondition(
                    field="document_id", operator=FilterOperator.IN, value=list(documents)
                ),
            ],
            should=list(result.should),
            must_not=list(result.must_not),
        )

    async def validate(
        self, candidates: tuple[VectorSearchResult, ...], context: StorageOperationContext
    ) -> tuple[VectorSearchResult, ...]:
        if self._repository is None:
            return candidates
        documents = tuple(
            dict.fromkeys(
                str(item.payload["document_id"])
                for item in candidates
                if item.payload.get("document_id")
            )
        )
        try:
            async with asyncio.timeout(1):
                allowed = await self._repository.authorized_document_ids(
                    str(context.tenant_id), documents, access=context.access
                )
        except Exception as error:
            logger.warning(
                "Final permission check unavailable", extra={"error_code": type(error).__name__}
            )
            return ()
        return tuple(item for item in candidates if item.payload.get("document_id") in allowed)
