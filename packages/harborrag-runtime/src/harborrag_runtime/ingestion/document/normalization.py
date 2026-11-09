"""Compatibility facade for canonical document normalization composition."""

from __future__ import annotations

from .normalizers import (
    SourceDocumentNormalizer,
    default_source_document_normalizer_builder,
)
from .normalizers import (
    SourceDocumentNormalizerBuilder as SourceDocumentNormalizerBuilder,
)
from .normalizers import (
    SourceNormalizerFactory as SourceNormalizerFactory,
)
from .normalizers import (
    SourceNormalizerRegistration as SourceNormalizerRegistration,
)

CANONICAL_NORMALIZER_VERSION = "canonical-v4"


def build_source_document_normalizer() -> SourceDocumentNormalizer:
    """Build the default router from independently registered providers."""

    return default_source_document_normalizer_builder().build()
