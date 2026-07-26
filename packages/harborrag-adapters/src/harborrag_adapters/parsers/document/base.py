"""Engine contract for word-processing and ebook document formats."""

from __future__ import annotations

from abc import ABC

from harborrag_adapters.parsers.common.base import HarborParserEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborDocumentEngine(
    HarborParserEngine[ParseInput, ParsedDocument],
    ABC,
):
    """Provider contract for DOCX, ODT, RTF, EPUB, and related formats."""
