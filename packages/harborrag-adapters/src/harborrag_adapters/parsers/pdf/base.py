"""Contract implemented by independent PDF provider engines."""

from __future__ import annotations

from abc import ABC, abstractmethod

from harborrag_adapters.parsers.common.models import ParseRequest
from harborrag_adapters.parsers.common.resources import request_to_parse_input
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_core.domain.parser import ParseInput


class HarborPDFEngine(ABC):
    """Provider contract for PDF parsing capabilities and health."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    def supports_ocr(self) -> bool:
        return False

    @property
    def supports_tables(self) -> bool:
        return False

    @property
    def supports_layout(self) -> bool:
        return False

    async def parse(self, request: ParseRequest) -> PDFParseResult:
        return self.parse_input(request_to_parse_input(request))

    @abstractmethod
    def parse_input(self, input: ParseInput) -> PDFParseResult:
        raise NotImplementedError

    async def healthcheck(self) -> bool:
        return True


__all__ = ["HarborPDFEngine"]
