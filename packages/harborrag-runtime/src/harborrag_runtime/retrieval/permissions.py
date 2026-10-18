"""Canonical document ACLs constrain vector ranking, then guard the final read."""

from __future__ import annotations

import asyncio
import logging

from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
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
        access = context.access
        conditions: list[VectorFilterCondition] = []
        if access.corpus_mode != "tenant_shared":
            documents = await self._allowed_documents(self._repository, context)
            conditions.append(
                VectorFilterCondition(
                    field="document_id", operator=FilterOperator.IN, value=list(documents)
                )
            )
        # The datasources the principal is entitled to apply in both corpus modes.
        # Every evidence point carries an indexed ``source_scope_id``, so this ranks
        # inside the set instead of filling the window with points the final check
        # would drop.
        if access.entitled_source_scopes is not None:
            conditions.append(
                VectorFilterCondition(
                    field="source_scope_id",
                    operator=FilterOperator.IN,
                    value=sorted(access.entitled_source_scopes),
                )
            )
        if not conditions:
            # The vector repository already scopes by tenant. The final canonical
            # publication check still runs in validate().
            return filters
        result = filters if filters is not None else VectorFilter()
        return VectorFilter(
            must=[*result.must, *conditions],
            should=list(result.should),
            must_not=list(result.must_not),
        )

    @staticmethod
    async def _allowed_documents(
        repository: TopologySearchPort, context: StorageOperationContext
    ) -> tuple[str, ...]:
        try:
            async with asyncio.timeout(2):
                return await repository.allowed_document_ids(
                    str(context.tenant_id), access=context.access, limit=10000
                )
        except Exception as error:
            logger.warning(
                "Permission scope unavailable", extra={"error_code": type(error).__name__}
            )
            raise HarborAuthorizationUnavailableError("AUTHORIZATION_UNAVAILABLE") from error

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
            raise HarborAuthorizationUnavailableError("AUTHORIZATION_UNAVAILABLE") from error
        return tuple(item for item in candidates if item.payload.get("document_id") in allowed)
