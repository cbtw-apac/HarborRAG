"""Dependency construction for the configured parser-family registry."""

from __future__ import annotations

from collections.abc import Iterable

from harborrag_adapters.parsers.common.base import HarborParser
from harborrag_adapters.parsers.common.config import ParserConfig
from harborrag_adapters.parsers.document.engines.docx.engine import DocxDocumentEngine
from harborrag_adapters.parsers.document.engines.epub.engine import EpubDocumentEngine
from harborrag_adapters.parsers.document.engines.odt.engine import OdtDocumentEngine
from harborrag_adapters.parsers.document.parser import HarborDocumentParser
from harborrag_adapters.parsers.errors import UnknownPDFEngineError
from harborrag_adapters.parsers.image.engines.ocr.engine import OcrImageEngine
from harborrag_adapters.parsers.image.parser import HarborImageParser
from harborrag_adapters.parsers.markup.engines.html.engine import HtmlMarkupEngine
from harborrag_adapters.parsers.markup.engines.markdown.engine import MarkdownMarkupEngine
from harborrag_adapters.parsers.markup.parser import HarborMarkupParser
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.config import (
    PDFParserConfig,
    PDFParserProfile,
    PDFRouterConfig,
)
from harborrag_adapters.parsers.pdf.engines.docling.config import DoclingPDFConfig
from harborrag_adapters.parsers.pdf.engines.docling.engine import DoclingPDFEngine
from harborrag_adapters.parsers.pdf.engines.liteparse.engine import LiteParsePDFEngine
from harborrag_adapters.parsers.pdf.engines.mineru.config import MinerUPDFConfig
from harborrag_adapters.parsers.pdf.engines.mineru.engine import MinerUPDFEngine
from harborrag_adapters.parsers.pdf.engines.paddleocr.config import PaddleOCRPDFConfig
from harborrag_adapters.parsers.pdf.engines.paddleocr.engine import PaddleOCRPDFEngine
from harborrag_adapters.parsers.pdf.engines.pymupdf.engine import PyMuPDFEngine
from harborrag_adapters.parsers.pdf.parser import HarborPDFParser
from harborrag_adapters.parsers.presentation.engines.python_pptx.engine import (
    PythonPptxPresentationEngine,
)
from harborrag_adapters.parsers.presentation.parser import HarborPresentationParser
from harborrag_adapters.parsers.registry import HarborParserRegistry
from harborrag_adapters.parsers.spreadsheet.engines.csv.engine import CsvSpreadsheetEngine
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.engine import (
    ExcelSpreadsheetEngine,
)
from harborrag_adapters.parsers.spreadsheet.parser import HarborSpreadsheetParser
from harborrag_adapters.parsers.structured.engines.json.engine import JsonStructuredEngine
from harborrag_adapters.parsers.structured.parser import HarborStructuredParser
from harborrag_adapters.parsers.text.engines.plain_text.engine import PlainTextEngine
from harborrag_adapters.parsers.text.parser import HarborTextParser


class HarborParserFactory:
    """Build parser families and register their externally supported formats."""

    def create_registry(
        self,
        config: ParserConfig | None = None,
        *,
        families: Iterable[HarborParser] | None = None,
    ) -> HarborParserRegistry:
        resolved_config = config or ParserConfig()
        registry = HarborParserRegistry()
        configured_families = (
            tuple(families) if families is not None else self._default_families(resolved_config)
        )
        for parser in configured_families:
            registry.register_family(parser)
        return registry

    def create_pdf_parser(
        self,
        config: PDFParserConfig | None = None,
        *,
        profile: PDFParserProfile | str | None = None,
    ) -> HarborPDFParser:
        """Construct the PDF workflow and its independent provider engines."""
        resolved_config = config or PDFParserConfig()
        profile_name = str(profile or resolved_config.router.default_profile)
        router_config = resolved_config.router
        if profile is not None and profile_name != router_config.default_profile:
            router_config = PDFRouterConfig(
                default_profile=profile_name,
                profiles=router_config.profiles,
            )
        engines = self._configured_pdf_engines(profile_name, router_config)
        return HarborPDFParser(
            engines=engines,
            min_content_chars=resolved_config.min_content_chars,
            profile=profile_name,
            router_config=router_config,
        )

    def _default_families(self, config: ParserConfig) -> tuple[HarborParser, ...]:
        pdf_config = config.pdf if isinstance(config.pdf, PDFParserConfig) else PDFParserConfig()
        families: list[HarborParser] = []
        if config.presentation.enabled:
            families.append(HarborPresentationParser((PythonPptxPresentationEngine(),)))
        if config.document.enabled:
            families.append(
                HarborDocumentParser(
                    (
                        DocxDocumentEngine(),
                        OdtDocumentEngine(),
                        EpubDocumentEngine(),
                    )
                )
            )
        if config.spreadsheet.enabled:
            families.append(
                HarborSpreadsheetParser(
                    (
                        ExcelSpreadsheetEngine(),
                        CsvSpreadsheetEngine(),
                    )
                )
            )
        families.append(self.create_pdf_parser(pdf_config))
        if config.image.enabled:
            families.append(HarborImageParser((OcrImageEngine(**config.image.options),)))
        if config.markup.enabled:
            families.append(HarborMarkupParser((HtmlMarkupEngine(), MarkdownMarkupEngine())))
        if config.structured.enabled:
            families.append(HarborStructuredParser((JsonStructuredEngine(),)))
        if config.text.enabled:
            families.append(HarborTextParser((PlainTextEngine(),)))
        return tuple(families)

    def _configured_pdf_engines(
        self,
        profile_name: str,
        router_config: PDFRouterConfig,
    ) -> tuple[HarborPDFEngine, ...]:
        try:
            profile = PDFParserProfile.normalize(profile_name)
        except ValueError:
            profile = PDFParserProfile.BALANCED
        pool = self._pdf_engine_pool(profile)
        configured: list[HarborPDFEngine] = []
        for engine_name in router_config.profiles[profile_name].engine_order:
            try:
                configured.append(pool[engine_name])
            except KeyError as error:
                raise UnknownPDFEngineError(engine_name) from error
        return tuple(configured)

    @staticmethod
    def _pdf_engine_pool(profile: PDFParserProfile) -> dict[str, HarborPDFEngine]:
        docling_config = DoclingPDFConfig(do_ocr=True, do_table_structure=True)
        mineru_config = MinerUPDFConfig(backend="pipeline")
        paddleocr_config = PaddleOCRPDFConfig(use_table_recognition=True)

        if profile in {PDFParserProfile.OCR, PDFParserProfile.OCR_FIRST}:
            docling_config = DoclingPDFConfig(
                force_full_page_ocr=True,
                do_table_structure=True,
            )
            paddleocr_config = PaddleOCRPDFConfig(
                use_doc_orientation_classify=True,
                use_doc_unwarping=True,
                use_textline_orientation=True,
                use_table_recognition=True,
            )
        elif profile in {PDFParserProfile.QUALITY, PDFParserProfile.SCIENTIFIC}:
            docling_config = DoclingPDFConfig(
                do_ocr=True,
                do_table_structure=True,
                table_do_cell_matching=True,
                accelerator_device="auto",
            )
            mineru_config = MinerUPDFConfig(backend="hybrid", effort="medium")
            paddleocr_config = PaddleOCRPDFConfig(
                use_doc_orientation_classify=True,
                use_doc_unwarping=True,
                use_textline_orientation=True,
                use_table_recognition=True,
                use_formula_recognition=True,
                use_chart_recognition=True,
                use_region_detection=True,
            )

        engines: tuple[HarborPDFEngine, ...] = (
            PyMuPDFEngine(),
            DoclingPDFEngine(docling_config),
            LiteParsePDFEngine(),
            MinerUPDFEngine(mineru_config),
            PaddleOCRPDFEngine(paddleocr_config),
        )
        return {engine.name: engine for engine in engines}
