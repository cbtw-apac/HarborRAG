"""Extension contracts for source-specific document normalizers."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.registry import (
    ConnectorProviderDefinition,
    connector_registry,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain import Document, DocumentElement, ParsedDocument, RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_engine.ingestion import BaseDocumentNormalizer
from harborrag_runtime.ingestion.document.normalizers import (
    SourceDocumentNormalizerBuilder,
    default_source_document_normalizer_builder,
)


class TitleNormalizer(BaseDocumentNormalizer):
    """Small third-party strategy used to prove provider extensibility."""

    def __init__(self, fallback: BaseDocumentNormalizer, title: str) -> None:
        self.fallback = fallback
        self.title = title

    def normalize(self, raw: RawDocument, parsed: ParsedDocument) -> Document:
        return replace(self.fallback.normalize(raw, parsed), title=self.title)


class TitleTransform:
    def __init__(self, title: str) -> None:
        self.title = title

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        del raw, parsed
        return replace(document, title=self.title)


class NotionConnector(BaseConnector):
    provider_name = "notion"

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        del query
        yield from ()

    def load(self, record: SourceRecord) -> RawDocument:
        del record
        raise NotImplementedError


def _raw(source_system: str) -> RawDocument:
    return RawDocument(
        id="custom://workspace/page-1",
        source="custom://workspace/page-1",
        content="source content",
        content_type="text/plain",
        metadata={"source_system": source_system, "title": "Generic title"},
    )


def _parsed() -> ParsedDocument:
    return ParsedDocument(
        content="parsed content",
        parser_name="text",
        elements=[
            DocumentElement(
                id="custom://workspace/page-1#content",
                type="paragraph",
                content="parsed content",
            )
        ],
    )


def test_builder_adds_a_new_source_without_changing_the_router() -> None:
    normalizer = (
        default_source_document_normalizer_builder()
        .register("notion", lambda fallback: TitleNormalizer(fallback, "Notion page"))
        .build()
    )

    document = normalizer.normalize(_raw("  NOTION  "), _parsed())

    assert document.title == "Notion page"
    assert normalizer.source_systems == ("confluence", "jira", "local", "notion")


def test_default_builder_discovers_a_connector_owned_transform() -> None:
    connector_registry.register_provider(
        ConnectorProviderDefinition(
            name="notion",
            provider_cls=NotionConnector,
            document_transform_factory=lambda: TitleTransform("Notion page"),
        )
    )
    try:
        normalizer = default_source_document_normalizer_builder().build()
        document = normalizer.normalize(_raw("notion"), _parsed())
    finally:
        connector_registry.unregister_provider("notion")

    assert document.title == "Notion page"
    assert "notion" in normalizer.source_systems


def test_each_build_is_an_immutable_snapshot_of_builder_registrations() -> None:
    builder = SourceDocumentNormalizerBuilder().register(
        "alpha", lambda fallback: TitleNormalizer(fallback, "Alpha")
    )
    first = builder.build()
    builder.register("beta", lambda fallback: TitleNormalizer(fallback, "Beta"))
    second = builder.build()

    assert first.source_systems == ("alpha",)
    assert second.source_systems == ("alpha", "beta")
    assert first.normalize(_raw("beta"), _parsed()).title == "Generic title"
    assert second.normalize(_raw("beta"), _parsed()).title == "Beta"


def test_builder_rejects_duplicate_and_invalid_provider_ownership() -> None:
    builder = SourceDocumentNormalizerBuilder().register(
        "Jira", lambda fallback: TitleNormalizer(fallback, "Jira")
    )

    with pytest.raises(ValueError, match="already registered"):
        builder.register(" jira ", lambda fallback: TitleNormalizer(fallback, "duplicate"))
    with pytest.raises(ValueError, match="source_system must start"):
        builder.register("jira/cloud", lambda fallback: TitleNormalizer(fallback, "invalid"))


def test_builder_validates_factory_results_at_composition_time() -> None:
    builder = SourceDocumentNormalizerBuilder().register("broken", lambda fallback: object())

    with pytest.raises(TypeError, match="must return BaseDocumentNormalizer"):
        builder.build()
