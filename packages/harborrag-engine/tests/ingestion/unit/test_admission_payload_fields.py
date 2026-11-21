"""Every document field a vector payload carries is part of the retrieval metadata hash."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
    canonical_document_bytes,
    load_canonical_document,
)
from harborrag_engine.ingestion import VectorProjectionBuilder, VectorProjectionInput
from harborrag_engine.ingestion.admission import (
    CanonicalVersionPlanner,
    PlannedDocumentVersion,
    SourceAdmissionPolicy,
)
from harborrag_engine.ingestion.chunking import ChunkingRequest

from .chunking_helpers import make_profile, make_service
from .test_vector_projection import chunk_set, representation_set

pytestmark = pytest.mark.unit

CREATED = datetime(2026, 1, 1, tzinfo=UTC)
UPDATED = datetime(2026, 1, 9, tzinfo=UTC)
PROCESSING = ProcessingProfile(
    parser_profile="text-v1",
    normalizer_version="canonical-v1",
    chunk_strategy="route-evidence-v3",
    dense_encoder_profile="dense-v1",
    sparse_encoder_profile="bm25-v1",
    graph_projection_version="graph-v1",
)
PAYLOAD_DESCRIPTOR_FIELDS = (
    "relative_path",
    "space_id",
    "page_id",
    "project_id",
    "issue_key",
    "attachment_id",
    "site_id",
    "drive_id",
    "item_id",
    "web_url",
    "status",
    "status_category",
    "priority",
    "author",
    "assignee",
    "reporter",
    "creator",
    "project_key",
    "project_name",
    "resolved_at",
    "due_date",
    "created_at",
    "updated_at",
    "source_created_at",
    "source_updated_at",
    "issue_type",
    "item_type",
    "labels",
    "components",
    "fields",
)
CONNECTORS = (
    ConnectorType.JIRA,
    ConnectorType.CONFLUENCE,
    ConnectorType.LOCAL,
    ConnectorType.SHAREPOINT,
    ConnectorType("github"),
)


def _document(
    connector: ConnectorType,
    *,
    updated_at: datetime = UPDATED,
    extra: dict[str, object] | None = None,
) -> Document:
    return Document(
        id=f"{connector.value}://DEMO/DEMO-1",
        title="DEMO-1: Migrate the build agents",
        content=[
            DocumentElement(
                id="description",
                type="paragraph",
                content="Move the build agents to the new runner pool before the freeze.",
                metadata={"field": "description"},
            )
        ],
        content_type="page",
        provenance=DocumentProvenance(
            source=connector.value,
            record_id="DEMO-1",
            created_at=CREATED,
            updated_at=updated_at,
            extra={"comments": [], "custom_fields": [], **(extra or {})},
        ),
    )


def _jira_issue(*, updated_at: datetime = UPDATED, **changes: object) -> Document:
    return _document(
        ConnectorType.JIRA,
        updated_at=updated_at,
        extra={
            "issue_key": "DEMO-1",
            "project_key": "DEMO",
            "issue_type": "Task",
            "status": "In Progress",
            "status_category": "In Progress",
            "priority": "High",
            "assignee": "Alex Example",
            "reporter": "Sam Example",
            "creator": "Sam Example",
            "due_date": "2026-02-01",
            "components": ["ci"],
            **changes,
        },
    )


def _plan(
    document: Document,
    connector: ConnectorType = ConnectorType.JIRA,
    *,
    source_version: str = "1",
) -> PlannedDocumentVersion:
    return CanonicalVersionPlanner().plan(
        document=document,
        source_identity=SourceIdentity(
            tenant_id="DEFAULT",
            connector_type=connector,
            connection_id="demo",
            source_item_id="DEMO-1",
            source_scope_id="DEMO",
            binding=SourceBinding(kind="ROOT"),
        ),
        admission=AdmissionSnapshot(source_version=source_version),
        processing=PROCESSING,
    )


def _active(planned: PlannedDocumentVersion) -> DocumentVersionSnapshot:
    return DocumentVersionSnapshot(
        document_id=planned.candidate.document_id,
        document_version_id=planned.candidate.document_version_id,
        fingerprints=planned.candidate.fingerprints,
        state=DocumentVersionState.ACTIVE,
    )


def _decision(active: PlannedDocumentVersion, later: PlannedDocumentVersion) -> object:
    return SourceAdmissionPolicy().after_normalization(
        active=_active(active), fingerprints=later.candidate.fingerprints
    )


def _payloads(planned: PlannedDocumentVersion, strategy: str) -> list[dict[str, object]]:
    """Return the evidence payloads the chunk stage builds from the stored document."""
    chunks = (
        make_service(
            make_profile(name=strategy, strategy=strategy, target=100, maximum=120),
            configuration_version="3",
        )
        .chunk(
            ChunkingRequest(
                tenant_id="DEFAULT",
                document_version_id=str(planned.candidate.document_version_id),
                connector_type=strategy,
                document=load_canonical_document(canonical_document_bytes(planned.document)),
            )
        )
        .chunks
    )
    batch = VectorProjectionBuilder().build(
        VectorProjectionInput(
            chunks=chunks,
            representations=representation_set(chunks),
            chunk_artifacts=chunk_set(chunks),
        )
    )
    return [record.payload.model_dump(mode="json") for record in batch.evidence_records]


def test_a_jira_metadata_only_change_is_metadata_changed() -> None:
    first = _plan(_jira_issue())
    later = _plan(
        _jira_issue(
            updated_at=datetime(2026, 1, 20, tzinfo=UTC),
            assignee="Sam Example",
            priority="Low",
            due_date="2026-03-01",
        ),
        source_version="2",
    )

    assert (
        first.candidate.fingerprints.canonical_content_hash
        == later.candidate.fingerprints.canonical_content_hash
    )
    assert _decision(first, later) == SourceAdmissionDecision.METADATA_CHANGED


@pytest.mark.parametrize(
    "change",
    (
        {"assignee": "Sam Example"},
        {"reporter": "Alex Example"},
        {"creator": "Alex Example"},
        {"priority": "Low"},
        {"status_category": "Done"},
        {"due_date": "2026-03-01"},
        {"resolved_at": "2026-02-20T10:00:00+00:00"},
        {"project_name": "Platform Engineering"},
        {"components": ["build-agents"]},
        {"fields": {"team": "Platform"}},
    ),
    ids=lambda change: next(iter(change)),
)
def test_a_jira_payload_field_change_changes_the_retrieval_hash(
    change: dict[str, object],
) -> None:
    (field,) = change
    first = _plan(_jira_issue())
    later = _plan(_jira_issue(**change), source_version="2")

    before = [payload.get(field) for payload in _payloads(first, "jira")]
    after = [payload.get(field) for payload in _payloads(later, "jira")]

    assert before != after
    assert _decision(first, later) == SourceAdmissionDecision.METADATA_CHANGED


@pytest.mark.parametrize(
    ("connector", "strategy"),
    ((ConnectorType.JIRA, "jira"), (ConnectorType.CONFLUENCE, "confluence")),
)
def test_an_update_time_change_reaches_the_payload_and_the_retrieval_hash(
    connector: ConnectorType,
    strategy: str,
) -> None:
    first = _plan(_document(connector), connector)
    later = _plan(_document(connector, updated_at=datetime(2026, 2, 1, tzinfo=UTC)), connector)

    assert {payload["updated_at"] for payload in _payloads(first, strategy)} == {
        UPDATED.isoformat()
    }
    assert {payload["updated_at"] for payload in _payloads(later, strategy)} == {
        "2026-02-01T00:00:00+00:00"
    }
    assert _decision(first, later) == SourceAdmissionDecision.METADATA_CHANGED


@pytest.mark.parametrize("connector", CONNECTORS, ids=lambda connector: connector.value)
def test_every_payload_descriptor_field_is_hashed_for_every_connector(
    connector: ConnectorType,
) -> None:
    unhashed = []
    for field in PAYLOAD_DESCRIPTOR_FIELDS:
        first = _plan(_document(connector, extra={field: "first"}), connector)
        later = _plan(_document(connector, extra={field: "second"}), connector)
        if _decision(first, later) != SourceAdmissionDecision.METADATA_CHANGED:
            unhashed.append(field)

    assert unhashed == []


@pytest.mark.parametrize("connector", CONNECTORS, ids=lambda connector: connector.value)
def test_a_creation_time_change_is_metadata_changed(connector: ConnectorType) -> None:
    first = _plan(_document(connector), connector)
    document = _document(connector)
    document.provenance.created_at = datetime(2025, 12, 1, tzinfo=UTC)

    assert _decision(first, _plan(document, connector)) == SourceAdmissionDecision.METADATA_CHANGED


def test_a_touched_local_file_is_metadata_changed() -> None:
    first = _plan(_document(ConnectorType.LOCAL), ConnectorType.LOCAL)
    touched = _plan(
        _document(ConnectorType.LOCAL, updated_at=datetime(2026, 7, 1, tzinfo=UTC)),
        ConnectorType.LOCAL,
        source_version="2",
    )

    assert (
        first.candidate.fingerprints.canonical_content_hash
        == touched.candidate.fingerprints.canonical_content_hash
    )
    assert _decision(first, touched) == SourceAdmissionDecision.METADATA_CHANGED


def test_a_reverted_assignee_gets_a_version_of_its_own() -> None:
    """A revert is a new version, so it never collides with the version it reverts to."""
    original = _plan(_jira_issue())
    reassigned = _plan(
        _jira_issue(updated_at=datetime(2026, 1, 10, tzinfo=UTC), assignee="Sam Example"),
        source_version="2",
    )
    reverted = _plan(_jira_issue(updated_at=datetime(2026, 1, 11, tzinfo=UTC)), source_version="3")

    versions = {
        planned.candidate.document_version_id for planned in (original, reassigned, reverted)
    }
    assert len(versions) == 3
    assert _decision(reassigned, reverted) == SourceAdmissionDecision.METADATA_CHANGED
