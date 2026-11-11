"""Canonical document ACLs constrain vector ranking, then guard the final read.

The enumeration budget is the most document ids one pre-query ACL condition lists,
the adapter's ceiling. Each authority lookup gets its own timeout, and the scope
fallback has a budget of its own rather than what the failed enumeration left of a
shared one. A principal found over the enumeration budget goes straight to the
scope condition for a while, and a bounded number of such principals are
remembered. One whose readable set has since shrunk is still served correctly
meanwhile, because the scope condition is a superset that ``validate`` narrows,
just less tightly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from harborrag_core.contracts.errors import (
    HarborAuthorizationUnavailableError,
    HarborConflictError,
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

_ENUMERATION_BUDGET = 10000
_LOOKUP_TIMEOUT_SECONDS = 2.0
_OVER_BUDGET_TTL_SECONDS = 60.0
_OVER_BUDGET_MEMORY = 1024

type _AccessKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class ScopedFilter:
    """A caller's filter with the pre-query ACL condition joined to it.

    ``exact`` says whether that condition admits only documents the principal
    may read. Past the enumeration budget it is the readable source scopes
    instead -- a superset -- and then anything computed from the raw search,
    before ``validate``, can describe documents the principal may not see.
    """

    filters: VectorFilter | None
    exact: bool


class RetrievalPermissions:
    """Join a caller's document ACL to a vector filter, then validate candidates against it.

    Args:
        repository: Canonical topology authority. ``None`` is for embedding-only
            callers, whose filters pass through unchanged; production composition
            always supplies it, and unknown permissions there deny.
        clock: Monotonic time source, replaceable in tests.
    """

    def __init__(
        self,
        repository: TopologySearchPort | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._over_budget: OrderedDict[_AccessKey, float] = OrderedDict()

    async def scope_filter(
        self, filters: VectorFilter | None, context: StorageOperationContext
    ) -> VectorFilter | None:
        """Return ``filters`` with the caller's pre-query ACL condition joined to it."""
        return (await self.scoped(filters, context)).filters

    async def scoped(
        self, filters: VectorFilter | None, context: StorageOperationContext
    ) -> ScopedFilter:
        """Join the caller's pre-query ACL condition to ``filters``.

        Tenant-shared corpora need no condition because the vector repository
        already scopes by tenant; the final canonical publication check still
        runs in ``validate``.

        Raises:
            HarborAuthorizationUnavailableError: The permission authority failed.
        """
        if self._repository is None:
            return ScopedFilter(filters, exact=True)
        if context.access.corpus_mode == "tenant_shared":
            return ScopedFilter(filters, exact=True)
        try:
            condition = await self._readable_condition(self._repository, context)
        except Exception as error:
            logger.warning(
                "Permission scope unavailable", extra={"error_code": type(error).__name__}
            )
            raise HarborAuthorizationUnavailableError("AUTHORIZATION_UNAVAILABLE") from error
        result = filters if filters is not None else VectorFilter()
        return ScopedFilter(
            VectorFilter(
                must=[*result.must, condition],
                should=list(result.should),
                must_not=list(result.must_not),
            ),
            exact=condition.field == "document_id",
        )

    async def _readable_condition(
        self, repository: TopologySearchPort, context: StorageOperationContext
    ) -> VectorFilterCondition:
        """Return the narrowest pre-query ACL condition the authority can enumerate.

        The readable documents themselves, while there are few enough to list.
        Past the enumeration budget the readable source scopes stand in: a
        document is readable only inside a readable scope, so that condition
        admits everything the principal may read, and ``validate`` removes the
        documents of those scopes they may not. A principal who can read a large
        corpus is served from it rather than refused -- and, once found over the
        budget, is not made to pay for the discarded enumeration again for a while.
        """

        tenant_id = str(context.tenant_id)
        key = (tenant_id, context.access.principal_id, context.access.corpus_mode)
        if not self._known_over_budget(key):
            try:
                async with asyncio.timeout(_LOOKUP_TIMEOUT_SECONDS):
                    documents = await repository.allowed_document_ids(
                        tenant_id, access=context.access, limit=_ENUMERATION_BUDGET
                    )
            except HarborConflictError:
                self._remember_over_budget(key)
            else:
                return VectorFilterCondition(
                    field="document_id", operator=FilterOperator.IN, value=list(documents)
                )
        async with asyncio.timeout(_LOOKUP_TIMEOUT_SECONDS):
            scopes = await repository.allowed_source_scope_ids(
                tenant_id, access=context.access, limit=_ENUMERATION_BUDGET
            )
        logger.info(
            "Readable documents exceed the enumeration budget; pre-filtering by source",
            extra={"source_scopes": len(scopes)},
        )
        return VectorFilterCondition(
            field="source_scope_id", operator=FilterOperator.IN, value=list(scopes)
        )

    def _known_over_budget(self, key: _AccessKey) -> bool:
        expires = self._over_budget.get(key)
        if expires is None:
            return False
        if expires <= self._clock():
            del self._over_budget[key]
            return False
        return True

    def _remember_over_budget(self, key: _AccessKey) -> None:
        self._over_budget[key] = self._clock() + _OVER_BUDGET_TTL_SECONDS
        self._over_budget.move_to_end(key)
        while len(self._over_budget) > _OVER_BUDGET_MEMORY:
            self._over_budget.popitem(last=False)

    async def validate(
        self, candidates: tuple[VectorSearchResult, ...], context: StorageOperationContext
    ) -> tuple[VectorSearchResult, ...]:
        """Keep the candidates whose documents are currently published and readable."""
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
