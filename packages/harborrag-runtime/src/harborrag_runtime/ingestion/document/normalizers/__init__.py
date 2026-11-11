"""Extensible connector-aware document normalization."""

from .builtins import default_source_document_normalizer_builder
from .transform import ConnectorTransformDocumentNormalizer

__all__ = [
    "ConnectorTransformDocumentNormalizer",
    "default_source_document_normalizer_builder",
]
