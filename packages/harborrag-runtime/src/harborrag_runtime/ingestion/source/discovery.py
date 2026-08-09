"""Authoritative source discovery planning."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from itertools import islice
from time import perf_counter

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.descriptors import (
    ConnectorDocumentDescriptor,
)
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.repositories.database import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.domain.source import SourceRecord

from .descriptor_mapping import PendingSourceRelease, SourceDescriptorMapper
from .models import (
    PlannedDocumentRelease,
    SourceDiscoveryPage,
    SourceIngestionRequest,
)

_LEGACY_ROOT_BATCH_SIZE = 50
_REGISTRATION_BATCH_SIZE = 200

logger = logging.getLogger("harborrag.runtime.ingestion.source_discovery")


class SourceDiscoveryPlanner:
    """Persist one authoritative scan and prepare globally dispatchable jobs."""

    def __init__(self, control: IngestionControlPlaneDatabase) -> None:
        self._control = control
        self._descriptors = SourceDescriptorMapper()

    async def discover(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
        *,
        scan_id: str,
    ) -> tuple[PlannedDocumentRelease, ...]:
        if bool(getattr(getattr(connector, "capabilities", None), "pagination", False)):
            return await self._discover_native_pages(request, connector, scan_id=scan_id)
        return await self._discover_legacy(request, connector, scan_id=scan_id)

    async def discover_page(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
        *,
        scan_id: str,
        cursor: str | None,
        page_size: int | None = None,
    ) -> SourceDiscoveryPage:
        """Fetch, describe, and register one native provider page."""

        provider_started = perf_counter()
        page = await asyncio.to_thread(
            connector.discover_page,
            request.query,
            cursor=cursor,
            page_size=page_size or request.discovery_page_size,
        )
        provider_seconds = perf_counter() - provider_started
        descriptor_started = perf_counter()
        descriptor_concurrency = self._descriptor_concurrency(
            connector,
            request.discovery_concurrency,
        )
        descriptors = await self._describe_roots(
            connector,
            page.records,
            concurrency=descriptor_concurrency,
        )
        descriptor_seconds = perf_counter() - descriptor_started
        pending = [
            release
            for descriptor in descriptors
            for release in self._descriptors.releases(request, descriptor)
        ]
        planned: list[PlannedDocumentRelease] = []
        while pending:
            batch = pending[:_REGISTRATION_BATCH_SIZE]
            del pending[:_REGISTRATION_BATCH_SIZE]
            planned.extend(await self._record_batch(scan_id, batch))
        logger.info(
            "Source discovery page task_id=%s roots=%d documents=%d has_next=%s "
            "provider_ms=%.1f descriptor_ms=%.1f concurrency=%d",
            request.task_id,
            len(page.records),
            len(planned),
            page.next_cursor is not None,
            provider_seconds * 1000,
            descriptor_seconds * 1000,
            descriptor_concurrency,
        )
        return SourceDiscoveryPage(
            planned=tuple(planned),
            next_cursor=page.next_cursor,
            root_count=len(page.records),
            provider_seconds=provider_seconds,
            descriptor_seconds=descriptor_seconds,
        )

    async def _discover_native_pages(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
        *,
        scan_id: str,
    ) -> tuple[PlannedDocumentRelease, ...]:
        planned: list[PlannedDocumentRelease] = []
        cursor: str | None = None
        root_count = 0
        while True:
            remaining = (
                request.query.limit - root_count if request.query.limit is not None else None
            )
            if remaining is not None and remaining <= 0:
                return tuple(planned)
            page = await self.discover_page(
                request,
                connector,
                scan_id=scan_id,
                cursor=cursor,
                page_size=(
                    min(request.discovery_page_size, remaining)
                    if remaining is not None
                    else request.discovery_page_size
                ),
            )
            planned.extend(page.planned)
            root_count += page.root_count
            await self._record_progress(request, root_count=root_count, planned=len(planned))
            if page.next_cursor is None or (
                request.query.limit is not None and root_count >= request.query.limit
            ):
                return tuple(planned)
            if page.next_cursor == cursor:
                raise ValueError("connector returned a non-advancing discovery cursor")
            cursor = page.next_cursor

    async def _discover_legacy(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
        *,
        scan_id: str,
    ) -> tuple[PlannedDocumentRelease, ...]:
        roots = iter(connector.discover(request.query))
        planned: list[PlannedDocumentRelease] = []
        pending: list[PendingSourceRelease] = []
        root_count = 0
        while root_batch := await asyncio.to_thread(self._next_roots, roots):
            descriptors = await self._describe_roots(
                connector,
                root_batch,
                concurrency=self._descriptor_concurrency(
                    connector,
                    request.discovery_concurrency,
                ),
            )
            for descriptor in descriptors:
                pending.extend(self._descriptors.releases(request, descriptor))
                root_count += 1
                while len(pending) >= _REGISTRATION_BATCH_SIZE:
                    batch = pending[:_REGISTRATION_BATCH_SIZE]
                    del pending[:_REGISTRATION_BATCH_SIZE]
                    planned.extend(await self._record_batch(scan_id, batch))
            if pending:
                planned.extend(await self._record_batch(scan_id, pending))
                pending.clear()
            await self._record_progress(request, root_count=root_count, planned=len(planned))
        return tuple(planned)

    @staticmethod
    async def _describe_roots(
        connector: BaseConnector | HarborConnector,
        roots: Sequence[SourceRecord],
        *,
        concurrency: int,
    ) -> tuple[ConnectorDocumentDescriptor, ...]:
        """Describe roots concurrently while preserving provider result order."""

        descriptors: list[ConnectorDocumentDescriptor] = []
        for offset in range(0, len(roots), concurrency):
            batch = roots[offset : offset + concurrency]
            descriptors.extend(
                await asyncio.gather(
                    *(asyncio.to_thread(connector.describe, root) for root in batch)
                )
            )
        return tuple(descriptors)

    async def _record_progress(
        self,
        request: SourceIngestionRequest,
        *,
        root_count: int,
        planned: int,
    ) -> None:
        await self._control.tasks.update_summary(
            request.task_id,
            {
                "stage": "DISCOVERING",
                "discovery_roots": root_count,
                "discovered": planned,
                "admitted": planned,
            },
        )
        logger.info(
            "Source discovery progress task_id=%s roots=%d documents=%d",
            request.task_id,
            root_count,
            planned,
        )

    @staticmethod
    def _descriptor_concurrency(
        connector: BaseConnector | HarborConnector,
        requested: int,
    ) -> int:
        capabilities = getattr(connector, "capabilities", None)
        return requested if bool(getattr(capabilities, "concurrent_describe", False)) else 1

    async def _record_batch(
        self,
        scan_id: str,
        pending: list[PendingSourceRelease],
    ) -> tuple[PlannedDocumentRelease, ...]:
        registrations = await self._control.source_scans.record_seen_many(
            scan_id=scan_id,
            items=tuple(item.item for item in pending),
        )
        return tuple(
            replace(
                item.release,
                request=replace(
                    item.release.request,
                    discovery_decision=registration.decision,
                ),
            )
            for item, registration in zip(pending, registrations, strict=True)
        )

    @staticmethod
    def _next_roots(roots: Iterator[SourceRecord]) -> tuple[SourceRecord, ...]:
        return tuple(islice(roots, _LEGACY_ROOT_BATCH_SIZE))
