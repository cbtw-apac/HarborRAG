"""Document-family parser and engine contract."""

from harborrag_adapters.parsers.document.base import HarborDocumentEngine
from harborrag_adapters.parsers.document.parser import HarborDocumentParser

__all__ = ["HarborDocumentEngine", "HarborDocumentParser"]
