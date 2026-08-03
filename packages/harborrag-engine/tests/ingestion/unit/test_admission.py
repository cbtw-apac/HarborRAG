from __future__ import annotations

from datetime import UTC, datetime

from harborrag_core.chunking import ConnectorType
from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    DocumentVersionSnapshot,
    DocumentVersionState,
    ProcessingProfile,
    SourceAdmissionDecision,
    SourceBinding,
    SourceIdentity,
)
from harborrag_engine.ingestion.admission import (
    CanonicalVersionPlanner,
    SourceAdmissionPolicy,
)


def _document(
    *,
    label: str = "ops",
    body: str = "Run it.",
    source: str = "confluence",
    updated_at: datetime | None = None,
    extra: dict[str, object] | None = None,
) -> Document:
    return Document(
        id="confluence://OPS/42",
        title="Guide",
        content=[
            DocumentElement(
                id="paragraph-1",
                type="paragraph",
                content=body,
            )
        ],
        content_type="page",
        provenance=DocumentProvenance(
            source=source,
            record_id="42",
            updated_at=updated_at or datetime(2026, 1, 1, tzinfo=UTC),
            extra={
                "labels": [label],
                "version": "7",
                "comments": [],
                **(extra or {}),
            },
        ),
    )


def _plan(document: Document):
    return CanonicalVersionPlanner().plan(
        document=document,
        source_identity=SourceIdentity(
            connector_type=ConnectorType.CONFLUENCE,
            connection_id="wiki-prod",
            source_item_id="42",
            source_scope_id="ops-space",
            binding=SourceBinding(kind="ROOT"),
        ),
        admission=AdmissionSnapshot(source_version="7"),
        processing=ProcessingProfile(
            parser_profile="html-v1",
            normalizer_version="canonical-v1",
            chunk_strategy="route-evidence-v3",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="bm25-v1",
            graph_projection_version="graph-v1",
        ),
    )


def test_metadata_changes_do_not_change_canonical_content_hash() -> None:
    first = _plan(_document(label="ops"))
    second = _plan(_document(label="production"))

    assert (
        first.candidate.fingerprints.canonical_content_hash
        == second.candidate.fingerprints.canonical_content_hash
    )
    assert (
        first.candidate.fingerprints.retrieval_metadata_hash
        != second.candidate.fingerprints.retrieval_metadata_hash
    )
    assert first.candidate.document_version_id != second.candidate.document_version_id


def test_planner_assigns_source_independent_document_identity() -> None:
    planned = _plan(_document())

    assert planned.document.id == planned.candidate.document_id
    assert planned.document.provenance.record_id == "42"
    assert planned.document.provenance.extra["source_scope_id"] == "ops-space"
    assert "workflow_id" not in planned.document.provenance.extra


def test_admission_policy_distinguishes_skip_metadata_and_content_changes() -> None:
    original = _plan(_document())
    snapshot = DocumentVersionSnapshot(
        document_id=original.candidate.document_id,
        document_version_id=original.candidate.document_version_id,
        fingerprints=original.candidate.fingerprints,
        state=DocumentVersionState.ACTIVE,
    )
    policy = SourceAdmissionPolicy()

    assert (
        policy.before_fetch(
            active=snapshot,
            admission_change_key=original.candidate.fingerprints.admission_change_key,
            processing_fingerprint=original.candidate.fingerprints.processing_fingerprint,
        )
        == SourceAdmissionDecision.UNCHANGED
    )
    assert (
        policy.after_normalization(
            active=snapshot,
            fingerprints=_plan(_document(label="production")).candidate.fingerprints,
        )
        == SourceAdmissionDecision.METADATA_CHANGED
    )
    assert (
        policy.after_normalization(
            active=snapshot,
            fingerprints=_plan(_document(body="Stop it.")).candidate.fingerprints,
        )
        == SourceAdmissionDecision.UPDATED
    )


def test_local_root_and_mtime_do_not_change_canonical_version_identity() -> None:
    planner = CanonicalVersionPlanner()
    source_identity = SourceIdentity(
        connector_type=ConnectorType.LOCAL,
        connection_id="mounted-docs",
        source_item_id="architecture/guide.md",
        source_scope_id="engineering-docs",
        binding=SourceBinding(kind="ROOT"),
    )
    processing = ProcessingProfile(
        parser_profile="markdown-v1",
        normalizer_version="canonical-v1",
        chunk_strategy="route-evidence-v3",
        dense_encoder_profile="dense-v1",
        sparse_encoder_profile="bm25-v1",
        graph_projection_version="graph-v1",
    )

    def plan(root: str, mtime: datetime):
        return planner.plan(
            document=_document(
                source=f"file://{root}/architecture/guide.md",
                updated_at=mtime,
                extra={
                    "path": f"{root}/architecture/guide.md",
                    "parent_path": f"{root}/architecture",
                    "relative_path": "architecture/guide.md",
                },
            ),
            source_identity=source_identity,
            admission=AdmissionSnapshot(source_version="content-sha256"),
            processing=processing,
        )

    first = plan("/mnt/one", datetime(2026, 1, 1, tzinfo=UTC))
    moved = plan("/srv/two", datetime(2026, 7, 1, tzinfo=UTC))

    assert first.candidate.document_id == moved.candidate.document_id
    assert first.candidate.document_version_id == moved.candidate.document_version_id
    assert (
        first.candidate.fingerprints.canonical_content_hash
        == moved.candidate.fingerprints.canonical_content_hash
    )
    assert first.document.provenance.source == "architecture/guide.md"
    assert first.document.provenance.url is None
    assert "path" not in first.document.provenance.extra
