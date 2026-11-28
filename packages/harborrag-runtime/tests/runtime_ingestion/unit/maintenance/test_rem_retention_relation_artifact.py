"""The retention purge finds relation artifacts by their key, not by the retired column."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store import ARTIFACT_BUCKET, IngestionArtifactLayout
from harborrag_core.base import utc_now
from harborrag_core.ingestion import (
    ArtifactReference,
    ChangeFingerprints,
    DocumentVersionSnapshot,
    DocumentVersionState,
    PurgeableDocumentVersion,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_runtime.ingestion import RetiredVersionPurgeService

pytestmark = pytest.mark.unit

DOCUMENT_ID = "document-1"
VERSION_ID = "version-1"


def _legacy_version(relation_key: str) -> PurgeableDocumentVersion:
    return PurgeableDocumentVersion(
        tenant_id="default",
        retired_at=utc_now(),
        snapshot=DocumentVersionSnapshot(
            document_id=DocumentId(DOCUMENT_ID),
            document_version_id=DocumentVersionId(VERSION_ID),
            fingerprints=ChangeFingerprints(
                admission_change_key="change-1",
                canonical_content_hash="canonical-1",
                retrieval_metadata_hash="metadata-1",
                processing_fingerprint="processing-1",
            ),
            state=DocumentVersionState.PURGING,
            relation_artifact=ArtifactReference(
                bucket=ARTIFACT_BUCKET,
                key=relation_key,
                sha256="c" * 64,
                byte_size=1,
                media_type="application/jsonl",
            ),
        ),
    )


def test_a_legacy_relation_artifact_is_still_deleted_by_its_deterministic_key() -> None:
    relations = IngestionArtifactLayout.relations(DOCUMENT_ID, VERSION_ID)

    targets = RetiredVersionPurgeService._recorded_objects(_legacy_version(relations))

    assert (ARTIFACT_BUCKET, relations) in targets


def test_the_retired_relation_column_is_not_read() -> None:
    targets = RetiredVersionPurgeService._recorded_objects(_legacy_version("legacy/elsewhere"))

    assert (ARTIFACT_BUCKET, "legacy/elsewhere") not in targets
