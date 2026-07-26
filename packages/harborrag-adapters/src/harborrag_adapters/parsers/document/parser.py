"""Complete workflow for word-processing and ebook documents."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import HarborSingleEngineFamilyParser
from harborrag_adapters.parsers.document.base import HarborDocumentEngine
from harborrag_adapters.parsers.document.normalization import DocumentNormalizer
from harborrag_adapters.parsers.document.router import DocumentEngineRouter


class HarborDocumentParser(HarborSingleEngineFamilyParser):
    """Select a document engine by format and normalize its output."""

    parser_name = "document"

    def __init__(
        self,
        engines: tuple[HarborDocumentEngine, ...],
    ) -> None:
        super().__init__(DocumentEngineRouter(engines), DocumentNormalizer())
