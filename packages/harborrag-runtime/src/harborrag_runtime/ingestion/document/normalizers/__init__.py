"""Extensible connector-aware document normalization."""

from .builder import (
    SourceDocumentNormalizerBuilder,
    SourceNormalizerFactory,
    SourceNormalizerRegistration,
)
from .builtins import default_source_document_normalizer_builder
from .router import SourceDocumentNormalizer
from .transform import ConnectorTransformDocumentNormalizer

__all__ = [
    "ConnectorTransformDocumentNormalizer",
    "SourceDocumentNormalizer",
    "SourceDocumentNormalizerBuilder",
    "SourceNormalizerFactory",
    "SourceNormalizerRegistration",
    "default_source_document_normalizer_builder",
]
