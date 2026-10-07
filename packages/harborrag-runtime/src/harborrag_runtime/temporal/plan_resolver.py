from __future__ import annotations

import asyncio
from collections import OrderedDict

from temporalio.exceptions import ApplicationError

from harborrag_core.ingestion import ArtifactReference
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion.source.models import PlannedDocumentRelease, SourcePlanIndex
from harborrag_runtime.ingestion.source.plan import SourcePlanRepository

from .conversion import to_artifact_reference
from .schemas import DocumentIngestionInput, RetryDocumentInput


class InvalidDocumentIndexError(ApplicationError):
    """Raised when a plan document index is negative or out of range."""

    def __init__(self) -> None:
        super().__init__("source plan document index is invalid", non_retryable=True)


class _Lru[K, V]:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._items: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        if key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key: K, value: V) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)


class PlanDocumentResolver:
    """Resolve one bounded document from an immutable source dispatch plan.

    A document workflow carries the plan reference and its index. The plan is
    stored twice: whole, and as the pages discovery produced plus an index of
    which page holds which indexes. Resolution reads the index once per plan
    and then only the page that holds the document, both cached per worker
    process, so the cost of one lookup stays bounded by a page rather than by
    the plan. A plan written without an index (a run started before this
    existed) falls back to the whole plan, cached, so it still finishes.
    """

    def __init__(
        self,
        plans: SourcePlanRepository,
        *,
        page_cache_size: int = 64,
        plan_cache_size: int = 2,
    ) -> None:
        self._plans = plans
        self._indexes: _Lru[str, SourcePlanIndex | None] = _Lru(16)
        self._pages: _Lru[tuple[str, int], tuple[PlannedDocumentRelease, ...]] = _Lru(
            page_cache_size
        )
        self._whole: _Lru[str, tuple[PlannedDocumentRelease, ...]] = _Lru(plan_cache_size)
        # One fill at a time keeps concurrent documents of the same page from
        # each downloading it; the pages are small, so the serialisation is cheap.
        self._fill = asyncio.Lock()

    async def get(
        self,
        request: DocumentIngestionInput | RetryDocumentInput,
    ) -> PlannedDocumentRelease:
        if request.document_index < 0:
            raise InvalidDocumentIndexError()
        reference = to_artifact_reference(request.plan_reference)
        context = StorageOperationContext.system(request.tenant_id)
        identity = SourcePlanRepository.plan_identity(reference)
        index = await self._index(reference, identity, context) if identity else None
        if index is None:
            planned = await self._whole_plan(reference, context)
            try:
                return planned[request.document_index]
            except IndexError as error:
                raise InvalidDocumentIndexError() from error
        try:
            page_number, offset = index.locate(request.document_index)
        except IndexError as error:
            raise InvalidDocumentIndexError() from error
        if identity is None:  # pragma: no cover - index implies identity
            raise InvalidDocumentIndexError()
        page = await self._page(reference, identity, page_number, context)
        try:
            return page[offset]
        except IndexError as error:
            raise InvalidDocumentIndexError() from error

    async def _index(
        self,
        reference: ArtifactReference,
        identity: tuple[str, str],
        context: StorageOperationContext,
    ) -> SourcePlanIndex | None:
        key = reference.key
        async with self._fill:
            cached = self._indexes.get(key)
            if cached is not None or key in self._indexes._items:
                return cached
            task_id, scan_id = identity
            index = await self._plans.find_index(task_id=task_id, scan_id=scan_id, context=context)
            self._indexes.put(key, index)
            return index

    async def _page(
        self,
        reference: ArtifactReference,
        identity: tuple[str, str],
        page_number: int,
        context: StorageOperationContext,
    ) -> tuple[PlannedDocumentRelease, ...]:
        key = (reference.key, page_number)
        async with self._fill:
            cached = self._pages.get(key)
            if cached is not None:
                return cached
            task_id, scan_id = identity
            page = await self._plans.get_page_documents(
                task_id=task_id, scan_id=scan_id, page_number=page_number, context=context
            )
            if page is None:
                raise InvalidDocumentIndexError()
            self._pages.put(key, page)
            return page

    async def _whole_plan(
        self, reference: ArtifactReference, context: StorageOperationContext
    ) -> tuple[PlannedDocumentRelease, ...]:
        async with self._fill:
            cached = self._whole.get(reference.key)
            if cached is not None:
                return cached
            planned = await self._plans.get(reference, context=context)
            self._whole.put(reference.key, planned)
            return planned
