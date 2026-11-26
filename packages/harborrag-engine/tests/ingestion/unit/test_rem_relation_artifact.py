from __future__ import annotations

from typing import Any, cast

import pytest

from harborrag_core.ingestion import (
    ArtifactReference,
    ChangeFingerprints,
    DocumentFailure,
    DocumentVersionSnapshot,
    DocumentVersionState,
    IngestionFailureClassifier,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_engine.ingestion.release.lifecycle import DocumentVersionLifecycle

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _reference(key: str) -> ArtifactReference:
    return ArtifactReference(
        bucket="artifacts",
        key=key,
        sha256="b" * 64,
        byte_size=1,
        media_type="application/jsonl",
    )


CANONICAL = _reference("documents/document-1/version-1/canonical.json")
LEGACY_RELATIONS = _reference("documents/document-1/version-1/relations.jsonl")


class Versions:
    def __init__(self, snapshot: DocumentVersionSnapshot) -> None:
        self.snapshot = snapshot

    async def get_version(self, document_version_id: str) -> DocumentVersionSnapshot:
        del document_version_id
        return self.snapshot

    async def transition(self, document_version_id: str, state: DocumentVersionState) -> None:
        del document_version_id
        self.snapshot = self.snapshot.model_copy(update={"state": state})


class Reliability:
    def __init__(self) -> None:
        self.failures: list[DocumentFailure] = []

    async def projection_manifest(self, document_version_id: str) -> None:
        del document_version_id

    async def record_failure(self, failure: DocumentFailure) -> None:
        self.failures.append(failure)

    async def enqueue_cleanup(self, *, document_id: str, document_version_id: str) -> None:
        del document_id, document_version_id


class Control:
    def __init__(self, snapshot: DocumentVersionSnapshot) -> None:
        self.document_versions = Versions(snapshot)
        self.reliability = Reliability()


def _legacy_snapshot() -> DocumentVersionSnapshot:
    return DocumentVersionSnapshot(
        document_id=DocumentId("document-1"),
        document_version_id=DocumentVersionId("version-1"),
        fingerprints=ChangeFingerprints(
            admission_change_key="change-1",
            canonical_content_hash="canonical-1",
            retrieval_metadata_hash="metadata-1",
            processing_fingerprint="processing-1",
        ),
        state=DocumentVersionState.REPRESENTATIONS_READY,
        canonical_artifact=CANONICAL,
        relation_artifact=LEGACY_RELATIONS,
    )


async def test_record_failure_leaves_out_a_legacy_relation_artifact() -> None:
    control = Control(_legacy_snapshot())
    lifecycle = DocumentVersionLifecycle(
        control=cast(Any, control),
        canonical_artifacts=cast(Any, None),
        chunk_reader=cast(Any, None),
        projection_artifacts=cast(Any, None),
        failures=IngestionFailureClassifier(),
    )

    await lifecycle.record_failure(
        document_id="document-1",
        document_version_id="version-1",
        stage="build_projections",
        error=RuntimeError("stage failed"),
    )

    (failure,) = control.reliability.failures
    assert failure.artifact_references == (CANONICAL,)
