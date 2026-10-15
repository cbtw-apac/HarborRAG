"""Immutable source dispatch-plan persistence."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import datetime

from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
)
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ArtifactReference,
    ProcessingProfile,
    SourceAdmissionDecision,
    SourceIdentity,
    reject_runtime_fields,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.serialization import to_json_value

from ..document.models import DocumentReleaseRequest
from .models import (
    PlannedDocumentRelease,
    PlannedDocuments,
    SourcePlanCheckpoint,
    SourcePlanIndex,
    SourcePlanPageRange,
)

_PLAN_KEY = re.compile(r"^source-plans/(?P<task_id>[^/]+)/(?P<scan_id>[^/]+)\.json$")
_INDEX_KEY = re.compile(r"^source-plans/(?P<task_id>[^/]+)/(?P<scan_id>[^/]+)/index\.json$")
# Legacy (non-paginated) discoveries hand over the whole plan at once; it is
# paged after the fact so document lookups stay bounded there too.
DEFAULT_PLAN_PAGE_SIZE = 500


class SourcePlanRepository:
    """Keep document dispatch payloads out of Temporal workflow history."""

    def __init__(
        self,
        writer: ImmutableArtifactWriter,
        reader: ImmutableArtifactReader,
    ) -> None:
        self._writer = writer
        self._reader = reader

    async def put(
        self,
        *,
        task_id: str,
        scan_id: str,
        planned: tuple[PlannedDocumentRelease, ...],
        context: StorageOperationContext,
    ) -> ArtifactReference:
        payload = [self._dump(item) for item in planned]
        reject_runtime_fields(payload)
        return await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.source_plan(task_id, scan_id),
                payload=json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                media_type="application/json",
                artifact_kind="source-dispatch-plan",
            ),
            context=context,
        )

    async def get(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> tuple[PlannedDocumentRelease, ...]:
        values = json.loads(await self._reader.get(reference, context=context))
        if not isinstance(values, list):
            raise ValueError("source dispatch plan must contain a JSON list")
        return tuple(self._load(value) for value in values)

    async def find(
        self,
        *,
        task_id: str,
        scan_id: str,
        context: StorageOperationContext,
    ) -> ArtifactReference | None:
        """Resolve an already-persisted dispatch plan after an activity replay.

        A plan is its page index: the index is written only once every page it
        names is, so finding it means the plan is complete. A run from before
        plans were paged has only the whole-plan artifact.
        """

        index = await self._reader.find(
            bucket=ARTIFACT_BUCKET,
            key=IngestionArtifactLayout.source_plan_index(task_id, scan_id),
            media_type="application/json",
            context=context,
        )
        if index is not None:
            return index
        return await self._reader.find(
            bucket=ARTIFACT_BUCKET,
            key=IngestionArtifactLayout.source_plan(task_id, scan_id),
            media_type="application/json",
            context=context,
        )

    @staticmethod
    def plan_identity(reference: ArtifactReference) -> tuple[str, str] | None:
        """``(task_id, scan_id)`` of a plan or plan-index reference, or None for any other key."""

        match = _PLAN_KEY.match(reference.key) or _INDEX_KEY.match(reference.key)
        if match is None:
            return None
        return match.group("task_id"), match.group("scan_id")

    async def documents(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> PlannedDocuments:
        """The plan behind ``reference``, read one persisted page at a time."""

        match = _INDEX_KEY.match(reference.key)
        if match is None:
            return PlannedDocuments.of(await self.get(reference, context=context))
        task_id, scan_id = match.group("task_id"), match.group("scan_id")
        index = self._parse_index(await self._reader.get(reference, context=context))

        async def pages(start: int) -> AsyncIterator[tuple[PlannedDocumentRelease, ...]]:
            for page in index.pages[start:]:
                documents = await self.get_page_documents(
                    task_id=task_id,
                    scan_id=scan_id,
                    page_number=page.page_number,
                    context=context,
                )
                if documents is None or len(documents) != page.count:
                    raise ValueError("source plan page is missing or disagrees with its index")
                yield documents

        return PlannedDocuments(document_count=index.document_count, read_pages=pages)

    async def put_index(
        self,
        *,
        task_id: str,
        scan_id: str,
        index: SourcePlanIndex,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        payload = {
            "document_count": index.document_count,
            "pages": [
                {"page_number": p.page_number, "start_index": p.start_index, "count": p.count}
                for p in index.pages
            ],
        }
        return await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.source_plan_index(task_id, scan_id),
                payload=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                media_type="application/json",
                artifact_kind="source-dispatch-plan-index",
            ),
            context=context,
        )

    async def find_index(
        self, *, task_id: str, scan_id: str, context: StorageOperationContext
    ) -> SourcePlanIndex | None:
        reference = await self._reader.find(
            bucket=ARTIFACT_BUCKET,
            key=IngestionArtifactLayout.source_plan_index(task_id, scan_id),
            media_type="application/json",
            context=context,
        )
        if reference is None:
            return None
        return self._parse_index(await self._reader.get(reference, context=context))

    @staticmethod
    def _parse_index(payload: bytes) -> SourcePlanIndex:
        value = json.loads(payload)
        pages = value.get("pages") if isinstance(value, dict) else None
        if not isinstance(pages, list):
            raise ValueError("source plan index is invalid")
        return SourcePlanIndex(
            document_count=int(value["document_count"]),
            pages=tuple(
                SourcePlanPageRange(
                    page_number=int(p["page_number"]),
                    start_index=int(p["start_index"]),
                    count=int(p["count"]),
                )
                for p in pages
            ),
        )

    async def get_page_documents(
        self,
        *,
        task_id: str,
        scan_id: str,
        page_number: int,
        context: StorageOperationContext,
    ) -> tuple[PlannedDocumentRelease, ...] | None:
        reference = await self.find_page(
            task_id=task_id, scan_id=scan_id, page_number=page_number, context=context
        )
        if reference is None:
            return None
        return (await self.get_page(reference, context=context)).planned

    async def put_pages_and_index(
        self,
        *,
        task_id: str,
        scan_id: str,
        planned: tuple[PlannedDocumentRelease, ...],
        context: StorageOperationContext,
        page_size: int = DEFAULT_PLAN_PAGE_SIZE,
    ) -> ArtifactReference:
        """Page an already complete plan and write its index; returns the index."""

        if page_size < 1:
            raise ValueError("plan page size must be positive")
        counts: list[int] = []
        for number, start in enumerate(range(0, len(planned), page_size)):
            page = planned[start : start + page_size]
            await self.put_page(
                task_id=task_id,
                scan_id=scan_id,
                page_number=number,
                checkpoint=SourcePlanCheckpoint(
                    planned=page, next_cursor=None, root_count=len(page)
                ),
                context=context,
            )
            counts.append(len(page))
        return await self.put_index(
            task_id=task_id,
            scan_id=scan_id,
            index=SourcePlanIndex.from_page_counts(counts),
            context=context,
        )

    async def put_page(
        self,
        *,
        task_id: str,
        scan_id: str,
        page_number: int,
        checkpoint: SourcePlanCheckpoint,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        """Persist one immutable provider-page checkpoint for activity retries."""

        payload = {
            "next_cursor": checkpoint.next_cursor,
            "planned": [self._dump(item) for item in checkpoint.planned],
            "root_count": checkpoint.root_count,
        }
        reject_runtime_fields(payload)
        return await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.source_plan_page(task_id, scan_id, page_number),
                payload=json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                media_type="application/json",
                artifact_kind="source-discovery-page",
            ),
            context=context,
        )

    async def get_page(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> SourcePlanCheckpoint:
        value = json.loads(await self._reader.get(reference, context=context))
        if not isinstance(value, dict) or not isinstance(value.get("planned"), list):
            raise ValueError("source discovery page checkpoint is invalid")
        cursor = value.get("next_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("source discovery page cursor is invalid")
        root_count = value.get("root_count")
        if not isinstance(root_count, int) or isinstance(root_count, bool) or root_count < 0:
            raise ValueError("source discovery page root count is invalid")
        return SourcePlanCheckpoint(
            planned=tuple(self._load(item) for item in value["planned"]),
            next_cursor=cursor,
            root_count=root_count,
        )

    async def find_page(
        self,
        *,
        task_id: str,
        scan_id: str,
        page_number: int,
        context: StorageOperationContext,
    ) -> ArtifactReference | None:
        return await self._reader.find(
            bucket=ARTIFACT_BUCKET,
            key=IngestionArtifactLayout.source_plan_page(task_id, scan_id, page_number),
            media_type="application/json",
            context=context,
        )

    @staticmethod
    def _dump(item: PlannedDocumentRelease) -> dict[str, object]:
        request = item.request
        source = request.source
        return {
            "document_id": item.document_id,
            "request": {
                "tenant_id": request.tenant_id,
                "connector_name": request.connector_name,
                "source": {
                    "id": source.id,
                    "source_type": source.source_type,
                    "locator": source.locator,
                    "metadata": to_json_value(source.metadata),
                    "updated_at": (
                        source.updated_at.isoformat() if source.updated_at is not None else None
                    ),
                    "checksum": source.checksum,
                },
                "source_identity": request.source_identity.model_dump(mode="json"),
                "admission": request.admission.model_dump(mode="json"),
                "processing": request.processing.model_dump(mode="json"),
                "configuration_fingerprint": request.configuration_fingerprint,
                "discovery_decision": (
                    request.discovery_decision.value
                    if request.discovery_decision is not None
                    else None
                ),
                "force_reprocess": request.force_reprocess,
            },
        }

    @staticmethod
    def _load(value: object) -> PlannedDocumentRelease:
        if not isinstance(value, dict) or not isinstance(
            value.get("request"),
            dict,
        ):
            raise ValueError("source dispatch plan record is invalid")
        request = value["request"]
        source = request.get("source")
        if not isinstance(source, dict):
            raise ValueError("source dispatch plan source is invalid")
        updated_at = source.get("updated_at")
        decision = request.get("discovery_decision")
        return PlannedDocumentRelease(
            document_id=str(value["document_id"]),
            request=DocumentReleaseRequest(
                tenant_id=str(request["tenant_id"]),
                connector_name=str(request["connector_name"]),
                source=SourceRecord(
                    id=str(source["id"]),
                    source_type=str(source["source_type"]),
                    locator=str(source["locator"]),
                    metadata=dict(source.get("metadata") or {}),
                    updated_at=(
                        datetime.fromisoformat(str(updated_at)) if updated_at is not None else None
                    ),
                    checksum=(
                        str(source["checksum"]) if source.get("checksum") is not None else None
                    ),
                ),
                source_identity=SourceIdentity.model_validate(request["source_identity"]),
                admission=AdmissionSnapshot.model_validate(request["admission"]),
                processing=ProcessingProfile.model_validate(request["processing"]),
                configuration_fingerprint=(
                    str(request["configuration_fingerprint"])
                    if request.get("configuration_fingerprint") is not None
                    else None
                ),
                discovery_decision=(
                    SourceAdmissionDecision(str(decision)) if decision is not None else None
                ),
                force_reprocess=bool(request.get("force_reprocess", False)),
            ),
        )
