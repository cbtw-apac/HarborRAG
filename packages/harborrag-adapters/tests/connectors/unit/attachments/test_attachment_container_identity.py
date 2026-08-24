"""An attachment binding inherits its parent's container identity."""

from __future__ import annotations

from harborrag_adapters.connectors.attachments import attachment_source_record
from harborrag_adapters.connectors.attachments.loading import AttachmentDocumentLoader
from harborrag_adapters.connectors.attachments.sources import AttachmentSourceDescriptor
from harborrag_adapters.connectors.confluence.normalization.schemas import (
    ConfluencePageInput,
    space_identity,
)
from harborrag_adapters.connectors.jira.mappers import project_identity
from harborrag_core.domain.source import SourceRecord


def _descriptor() -> AttachmentSourceDescriptor:
    return AttachmentSourceDescriptor(
        attachment_id="att-1",
        title="diagram.png",
        media_type="image/png",
        size_bytes=12,
        download_url="https://example.invalid/att-1",
        source_version="v1",
        status="admitted",
    )


def _parent(item_id: str) -> SourceRecord:
    return SourceRecord(id=item_id, source_type="text/html", locator="x", metadata={})


def test_a_confluence_attachment_carries_the_space_its_parent_lives_in() -> None:
    """Without this the projector finds no container and files the attachment under the
    data source: measured on a live graph, 147 of 147 attachments hung off the DataSource
    while every page hung off the space."""

    inherited = space_identity({"id": "92045319", "key": "HARBORRAG"})

    record = attachment_source_record(
        _parent("confluence://HARBORRAG/91980595"),
        _descriptor(),
        inherited=inherited,
    )

    assert record.metadata["space_id"] == "92045319"
    assert record.metadata["space_key"] == "HARBORRAG"


def test_the_inherited_space_id_is_the_one_the_page_itself_projects() -> None:
    """Both sides must agree, or the space forks into two nodes: the source-entity node
    key hashes the provider id, and the page uses space_id in preference to space_key."""

    payload = {
        "id": "91980595",
        "title": "Technical Document",
        "space": {"id": "92045319", "key": "HARBORRAG"},
        "version": {"number": 3},
    }
    page = ConfluencePageInput.from_api_payload(payload, source_url="https://example.invalid")
    record = attachment_source_record(
        _parent("confluence://HARBORRAG/91980595"),
        _descriptor(),
        inherited=space_identity(payload["space"]),
    )

    assert record.metadata["space_id"] == page.space_id
    assert record.metadata["space_key"] == page.space_key


def test_a_jira_attachment_carries_its_parent_project() -> None:
    issue = {"key": "AMAST-7", "fields": {"project": {"key": "AMAST"}}}

    record = attachment_source_record(
        _parent("jira://AMAST/AMAST-7"),
        _descriptor(),
        inherited=project_identity(issue),
    )

    assert record.metadata["project_key"] == "AMAST"


def test_a_jira_project_falls_back_to_the_issue_key_prefix() -> None:
    assert project_identity({"key": "AMAST-7", "fields": {}}) == {"project_key": "AMAST"}


def test_inherited_keys_can_never_overwrite_the_binding_identity() -> None:
    record = attachment_source_record(
        _parent("confluence://HARBORRAG/1"),
        _descriptor(),
        inherited={
            "binding_kind": "ROOT",
            "parent_source_item_id": "spoofed",
            "attachment_id": "spoofed",
        },
    )

    assert record.metadata["binding_kind"] == "ATTACHMENT"
    assert record.metadata["parent_source_item_id"] == "confluence://HARBORRAG/1"
    assert record.metadata["attachment_id"] == "att-1"


def test_omitting_inherited_identity_keeps_todays_behaviour() -> None:
    record = attachment_source_record(_parent("confluence://HARBORRAG/1"), _descriptor())

    assert "space_id" not in record.metadata
    assert record.metadata["binding_kind"] == "ATTACHMENT"


class _Gateway:
    def fetch(self, descriptor: AttachmentSourceDescriptor) -> bytes:
        del descriptor
        return b"bytes"


def test_the_loader_forwards_container_identity_into_the_raw_document() -> None:
    """Putting the keys on the SourceRecord is not enough on its own.

    AttachmentDocumentLoader rebuilds the raw document's metadata from the descriptor, so
    a key it does not forward never reaches provenance.extra and the projector never sees
    a container. Measured before this line existed: the descriptor stored space_id
    correctly and 49 of 49 attachments still hung off the DataSource.
    """

    record = attachment_source_record(
        _parent("confluence://HARBORRAG/91980595"),
        _descriptor(),
        inherited=space_identity({"id": "92045319", "key": "HARBORRAG"}),
    )

    raw = AttachmentDocumentLoader(_Gateway()).load(record)  # type: ignore[arg-type]

    assert raw.metadata["space_id"] == "92045319"
    assert raw.metadata["space_key"] == "HARBORRAG"


def test_the_loader_keeps_owning_the_keys_it_derives_from_the_descriptor() -> None:
    """Inherited keys are forwarded first, so they can never shadow loader-owned ones."""

    record = attachment_source_record(
        _parent("confluence://HARBORRAG/1"),
        _descriptor(),
        inherited={"space_key": "HARBORRAG", "attachment_id": "spoofed", "title": "spoofed"},
    )

    raw = AttachmentDocumentLoader(_Gateway()).load(record)  # type: ignore[arg-type]

    assert raw.metadata["attachment_id"] == "att-1"
    assert raw.metadata["title"] == "diagram.png"
    assert raw.metadata["space_key"] == "HARBORRAG"


def test_an_attachment_with_no_inherited_identity_loads_unchanged() -> None:
    raw = AttachmentDocumentLoader(_Gateway()).load(  # type: ignore[arg-type]
        attachment_source_record(_parent("confluence://HARBORRAG/1"), _descriptor())
    )

    assert "space_id" not in raw.metadata
    assert raw.metadata["attachment_id"] == "att-1"
