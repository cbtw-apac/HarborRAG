"""PDF parser family, provider contract, and routing policy."""

from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.config import (
    PDFParserConfig,
    PDFParserProfile,
    PDFProfileConfig,
    PDFRouterConfig,
)
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_adapters.parsers.pdf.parser import HarborPDFParser
from harborrag_adapters.parsers.pdf.quality import PDFQualityEvaluator
from harborrag_adapters.parsers.pdf.router import PDFEngineRegistry, PDFEngineRouter

__all__ = [
    "HarborPDFEngine",
    "HarborPDFParser",
    "PDFEngineRegistry",
    "PDFEngineRouter",
    "PDFParseResult",
    "PDFParserConfig",
    "PDFParserProfile",
    "PDFProfileConfig",
    "PDFQualityEvaluator",
    "PDFRouterConfig",
]
