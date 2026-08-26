"""Compatibility facade for canonical document normalization composition."""

from __future__ import annotations

from .normalizers import (
    SourceDocumentNormalizer,
    SourceDocumentNormalizerBuilder,
    SourceNormalizerFactory,
    SourceNormalizerRegistration,
    default_source_document_normalizer_builder,
)

CANONICAL_NORMALIZER_VERSION = "canonical-v4"


def build_source_document_normalizer() -> SourceDocumentNormalizer:
    """Build the default router from independently registered providers."""

    return default_source_document_normalizer_builder().build()


__all__ = [
    "CANONICAL_NORMALIZER_VERSION",
    "SourceDocumentNormalizer",
    "SourceDocumentNormalizerBuilder",
    "SourceNormalizerFactory",
    "SourceNormalizerRegistration",
    "build_source_document_normalizer",
]
