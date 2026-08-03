"""Built-in source normalizer registrations."""

from __future__ import annotations

from collections.abc import Callable

from harborrag_adapters.connectors import connector_registry
from harborrag_adapters.connectors.document_transform import (
    ConnectorDocumentTransformFactory,
)
from harborrag_engine.ingestion import BaseDocumentNormalizer

from .builder import SourceDocumentNormalizerBuilder
from .transform import ConnectorTransformDocumentNormalizer


def default_source_document_normalizer_builder() -> SourceDocumentNormalizerBuilder:
    """Create a builder containing HarborRAG's built-in source providers."""

    builder = SourceDocumentNormalizerBuilder()
    for provider_name in connector_registry.canonical_names():
        definition = connector_registry.get_definition(provider_name)
        if definition.document_transform_factory is not None:
            builder.register(
                provider_name,
                _normalizer_factory(definition.document_transform_factory),
            )
    return builder


def _normalizer_factory(
    transform_factory: ConnectorDocumentTransformFactory,
) -> Callable[[BaseDocumentNormalizer], BaseDocumentNormalizer]:
    def build(fallback: BaseDocumentNormalizer) -> BaseDocumentNormalizer:
        return ConnectorTransformDocumentNormalizer(
            fallback=fallback,
            transform=transform_factory(),
        )

    return build
