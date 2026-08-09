"""Connector fakes shared by ingestion tests."""

from __future__ import annotations

from types import SimpleNamespace

from harborrag_adapters.connectors.descriptors import ConnectorDocumentDescriptor
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import AdmissionSnapshot, SourceObjectVersion


class SourceConnector:
    def __init__(self) -> None:
        self.loads = 0
        self.body = "The worker timeout is 30 seconds."
        self.labels = ["operations"]

    def load(self, record: SourceRecord) -> RawDocument:
        self.loads += 1
        return RawDocument(
            id=record.id,
            source=record.locator,
            content=self.body,
            content_type="text/plain",
            metadata={
                "title": "Worker guide",
                "labels": list(self.labels),
                "checksum": f"source-{self.loads}",
            },
        )


class DescriptorConnector(SourceConnector):
    def discover(self, query):
        del query
        yield SourceRecord(
            id="docs/worker.txt",
            source_type="text/plain",
            locator="file:///docs/worker.txt",
            metadata={"relative_path": "docs/worker.txt"},
        )

    def describe(self, record: SourceRecord) -> ConnectorDocumentDescriptor:
        attachment = SourceRecord(
            id=f"{record.id}/attachments/a1",
            source_type="text/plain",
            locator="a1",
            metadata={
                "binding_kind": "ATTACHMENT",
                "relative_path": f"{record.id}/attachments/a1",
                "parent_source_item_id": record.id,
                "source_version": "attachment-1",
                "title": "notes.txt",
            },
        )
        record.metadata["relations"] = [
            {
                "predicate": "has_attachment",
                "target_id": attachment.id,
                "target_type": "document",
            }
        ]
        return ConnectorDocumentDescriptor(
            source=record,
            admission=AdmissionSnapshot(
                source_version="root-1",
                attachments=(
                    SourceObjectVersion(
                        source_item_id="a1",
                        source_version="attachment-1",
                    ),
                ),
            ),
            bound_records=(attachment,),
        )

    def load(self, record: SourceRecord) -> RawDocument:
        if record.metadata.get("binding_kind") != "ATTACHMENT":
            return super().load(record)
        self.loads += 1
        return RawDocument(
            id=record.id,
            source=record.locator,
            content=b"Attachment evidence.",
            content_type="text/plain",
            metadata={
                "title": "notes.txt",
                "source_version": "attachment-1",
                "relations": [
                    {
                        "predicate": "attached_to",
                        "target_id": record.metadata["parent_source_item_id"],
                        "target_type": "document",
                    }
                ],
            },
        )


class LinkedDocumentsConnector(SourceConnector):
    def discover(self, query):
        del query
        yield SourceRecord(
            id="docs/a.txt",
            source_type="text/plain",
            locator="file:///docs/a.txt",
            metadata={"relative_path": "docs/a.txt"},
        )
        yield SourceRecord(
            id="docs/b.txt",
            source_type="text/plain",
            locator="file:///docs/b.txt",
            metadata={"relative_path": "docs/b.txt"},
        )

    def describe(self, record: SourceRecord) -> ConnectorDocumentDescriptor:
        if record.id.endswith("a.txt"):
            record.metadata["relations"] = [
                {
                    "predicate": "links_to",
                    "target_id": "docs/b.txt",
                    "target_type": "document",
                }
            ]
        return ConnectorDocumentDescriptor(
            source=record,
            admission=AdmissionSnapshot(source_version=f"{record.id}-v1"),
        )

    def load(self, record: SourceRecord) -> RawDocument:
        raw = super().load(record)
        return RawDocument(
            id=raw.id,
            source=raw.source,
            content=raw.content,
            content_type=raw.content_type,
            metadata={**raw.metadata, **record.metadata},
        )


class TextParser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, raw: RawDocument) -> ParsedDocument:
        self.calls += 1
        return ParsedDocument(
            content=raw.text(),
            parser_name="text",
            parser_version="1",
            elements=[
                DocumentElement(
                    id="paragraph-1",
                    type="paragraph",
                    content=raw.text(),
                )
            ],
        )


class DeterministicEmbedClient:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def aembed(self, *, request):
        self.inputs.extend(request.inputs)
        return SimpleNamespace(
            embeddings=tuple(
                SimpleNamespace(
                    index=index,
                    value=(float(len(text)), 1.0, 0.5),
                )
                for index, text in enumerate(request.inputs)
            )
        )
