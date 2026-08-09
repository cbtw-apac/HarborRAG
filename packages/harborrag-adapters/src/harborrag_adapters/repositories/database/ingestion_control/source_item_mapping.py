from __future__ import annotations

from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    BindingKind,
    DiscoveredSourceItem,
    SourceAdmissionDecision,
    SourceBinding,
    SourceIdentity,
    SourceItemRegistration,
    StoredSourceItem,
)
from harborrag_core.schemas.ids import DocumentId

from .row_values import (
    DatabaseRow,
    optional_text,
    required_bool,
    required_mapping,
    required_text,
)


def registration_from_existing(
    existing: DatabaseRow | None,
    item: DiscoveredSourceItem,
) -> SourceItemRegistration:
    if existing is None:
        return SourceItemRegistration(decision=SourceAdmissionDecision.NEW)
    previous_descriptor = required_mapping(existing, "descriptor")
    if existing["admission_change_key"] != item.admission_change_key:
        decision = SourceAdmissionDecision.UPDATED
    elif previous_descriptor != item.descriptor:
        decision = SourceAdmissionDecision.METADATA_CHANGED
    else:
        decision = SourceAdmissionDecision.UNCHANGED
    return SourceItemRegistration(
        decision=decision,
        previous_source_version=str(existing["source_version"]),
        previous_admission_change_key=str(existing["admission_change_key"]),
        previous_descriptor=previous_descriptor,
    )


def stored_source_item_from_row(row: DatabaseRow) -> StoredSourceItem:
    return StoredSourceItem(
        source_identity=SourceIdentity(
            tenant_id=required_text(row, "tenant_id"),
            connector_type=ConnectorType(required_text(row, "connector_type")),
            connection_id=required_text(row, "connection_id"),
            source_item_id=required_text(row, "source_item_id"),
            source_scope_id=required_text(row, "source_scope_id"),
            binding=SourceBinding(
                kind=BindingKind(required_text(row, "binding_kind")),
                parent_source_item_id=optional_text(
                    row,
                    "parent_source_item_id",
                ),
            ),
        ),
        document_id=DocumentId(required_text(row, "document_id")),
        source_version=required_text(row, "source_version"),
        admission_change_key=required_text(
            row,
            "admission_change_key",
        ),
        descriptor=required_mapping(row, "descriptor"),
        active=required_bool(row, "is_active"),
    )
