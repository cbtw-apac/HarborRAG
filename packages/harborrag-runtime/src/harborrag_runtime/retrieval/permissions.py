"""Canonical document ACLs constrain vector ranking, then guard the final read."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from harborrag_core.contracts.errors import (
    HarborAuthorizationUnavailableError,
    HarborLimitExceededError,
)
from harborrag_core.indexing import (
    FilterOperator,
    VectorFilter,
    VectorFilterCondition,
    VectorSearchResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.search import TopologySearchPort

logger = logging.getLogger("harborrag.runtime.retrieval.permissions")


@dataclass(frozen=True, slots=True)
class PermissionScope:
    """The search filter plus whether it confines the search to readable documents.

    ``readable_only`` is false only when the reader can read more documents than
    the prefilter budget enumerates: the search then also sees unreadable
    documents, so anything it counts before ``validate()`` must stay private.
    """

    filters: VectorFilter | None
    readable_only: bool


class RetrievalPermissions:
    def __init__(self, repository: TopologySearchPort | None) -> None:
        self._repository = repository

    async def scope_filter(
        self, filters: VectorFilter | None, context: StorageOperationContext
    ) -> VectorFilter | None:
        return (await self.scope(filters, context)).filters

    async def scope(
        self, filters: VectorFilter | None, context: StorageOperationContext
    ) -> PermissionScope:
        if self._repository is None:
            # Explicit legacy embedding-only callers. Production composition
            # always supplies canonical authority; unknown permissions there deny.
            return PermissionScope(filters, readable_only=True)
        if context.access.corpus_mode == "tenant_shared":
            # The vector repository already scopes by tenant. The final canonical
            # publication check still runs in validate().
            return PermissionScope(filters, readable_only=True)
        try:
            async with asyncio.timeout(2):
                documents = await self._repository.allowed_document_ids(
                    str(context.tenant_id), access=context.access, limit=10000
                )
        except HarborLimitExceededError:
            # More readable documents than the prefilter budget. Search the
            # caller's filters unchanged: validate() still re-checks every
            # candidate against canonical ACLs before anything is returned.
            logger.info(
                "Permission scope over budget; relying on final permission check",
                extra={"reason": "permission_scope_over_budget"},
            )
            return PermissionScope(filters, readable_only=False)
        except Exception as error:
            logger.warning(
                "Permission scope unavailable", extra={"error_code": type(error).__name__}
            )
            raise HarborAuthorizationUnavailableError("AUTHORIZATION_UNAVAILABLE") from error
        result = filters if filters is not None else VectorFilter()
        scoped = VectorFilter(
            must=[
                *result.must,
                VectorFilterCondition(
                    field="document_id", operator=FilterOperator.IN, value=list(documents)
                ),
            ],
            should=list(result.should),
            must_not=list(result.must_not),
        )
        return PermissionScope(scoped, readable_only=True)

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
