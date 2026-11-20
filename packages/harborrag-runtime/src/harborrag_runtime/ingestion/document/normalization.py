"""Canonical normalizer version and construction of the default source router."""

from __future__ import annotations

from harborrag_engine.ingestion.normalizers.router import SourceDocumentNormalizer

from .normalizers import default_source_document_normalizer_builder

CANONICAL_NORMALIZER_VERSION = "canonical-v4"


def build_source_document_normalizer() -> SourceDocumentNormalizer:
    """Build the default router from independently registered providers."""
    return default_source_document_normalizer_builder().build()
