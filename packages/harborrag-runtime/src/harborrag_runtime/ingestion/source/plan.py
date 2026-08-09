"""Immutable source dispatch-plan persistence."""

from __future__ import annotations

import json
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
from .models import PlannedDocumentRelease, SourcePlanCheckpoint


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
        """Resolve an already-persisted dispatch plan after an activity replay."""

        return await self._reader.find(
            bucket=ARTIFACT_BUCKET,
            key=IngestionArtifactLayout.source_plan(task_id, scan_id),
            media_type="application/json",
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
