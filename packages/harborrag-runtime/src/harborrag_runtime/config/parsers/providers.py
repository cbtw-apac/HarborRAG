from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from harborrag_adapters.parsers.common.base import HarborParser
from harborrag_adapters.parsers.common.limits import PDF_MAX_PAGES
from harborrag_adapters.parsers.document.engines.docx.engine import DocxDocumentEngine
from harborrag_adapters.parsers.document.engines.epub.engine import EpubDocumentEngine
from harborrag_adapters.parsers.document.parser import HarborDocumentParser
from harborrag_adapters.parsers.factory import HarborParserFactory
from harborrag_adapters.parsers.image.engines.ocr.engine import OcrImageEngine
from harborrag_adapters.parsers.image.parser import HarborImageParser
from harborrag_adapters.parsers.markup.engines.html.engine import HtmlMarkupEngine
from harborrag_adapters.parsers.markup.engines.markdown.engine import MarkdownMarkupEngine
from harborrag_adapters.parsers.markup.parser import HarborMarkupParser
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.config import PDFParserConfig, PDFRouterConfig
from harborrag_adapters.parsers.pdf.engines.docling.config import (
    DoclingPDFConfig,
)
from harborrag_adapters.parsers.pdf.engines.docling.engine import (
    DoclingPDFEngine,
)
from harborrag_adapters.parsers.pdf.engines.liteparse.config import (
    LiteParsePDFConfig,
)
from harborrag_adapters.parsers.pdf.engines.liteparse.engine import (
    LiteParsePDFEngine,
)
from harborrag_adapters.parsers.pdf.engines.llm import LlmPDFConfig, LlmPDFEngine
from harborrag_adapters.parsers.pdf.engines.pymupdf.engine import PyMuPDFEngine
from harborrag_adapters.parsers.pdf.parser import HarborPDFParser
from harborrag_adapters.parsers.presentation.engines.python_pptx.engine import (
    PythonPptxPresentationEngine,
)
from harborrag_adapters.parsers.presentation.parser import HarborPresentationParser
from harborrag_adapters.parsers.spreadsheet.engines.csv.engine import (
    CsvSpreadsheetEngine,
)
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.engine import (
    ExcelSpreadsheetEngine,
)
from harborrag_adapters.parsers.spreadsheet.parser import HarborSpreadsheetParser
from harborrag_adapters.parsers.structured.engines.json.engine import (
    JsonStructuredEngine,
)
from harborrag_adapters.parsers.structured.parser import HarborStructuredParser
from harborrag_adapters.parsers.text.engines.plain_text.engine import PlainTextEngine
from harborrag_adapters.parsers.text.parser import HarborTextParser
from harborrag_core.ports.model_clients import HarborChatClientProtocol

type ParserFactory = Callable[..., HarborParser]
type PdfBackendFactory = Callable[..., HarborPDFEngine]
type OptionFactory = Callable[..., object]


def _document_parser(engine: object) -> HarborDocumentParser:
    return HarborDocumentParser((engine,))  # type: ignore[arg-type]


def _spreadsheet_parser(engine: object) -> HarborSpreadsheetParser:
    return HarborSpreadsheetParser((engine,))  # type: ignore[arg-type]


def _presentation_parser(engine: object) -> HarborPresentationParser:
    return HarborPresentationParser((engine,))  # type: ignore[arg-type]


def _markup_parser(engine: object) -> HarborMarkupParser:
    return HarborMarkupParser((engine,))  # type: ignore[arg-type]


def _structured_parser(engine: object) -> HarborStructuredParser:
    return HarborStructuredParser((engine,))  # type: ignore[arg-type]


def _text_parser(engine: object) -> HarborTextParser:
    return HarborTextParser((engine,))  # type: ignore[arg-type]


def _csv_factory(**settings: Any) -> HarborSpreadsheetParser:
    return _spreadsheet_parser(CsvSpreadsheetEngine(**settings))


def _docx_factory(**settings: Any) -> HarborDocumentParser:
    return _document_parser(DocxDocumentEngine(**settings))


def _epub_factory(**settings: Any) -> HarborDocumentParser:
    return _document_parser(EpubDocumentEngine(**settings))


def _excel_factory(**settings: Any) -> HarborSpreadsheetParser:
    return _spreadsheet_parser(ExcelSpreadsheetEngine(**settings))


def _html_factory(**settings: Any) -> HarborMarkupParser:
    return _markup_parser(HtmlMarkupEngine(**settings))


def _markdown_factory(**settings: Any) -> HarborMarkupParser:
    return _markup_parser(MarkdownMarkupEngine(**settings))


def _pptx_factory(**settings: Any) -> HarborPresentationParser:
    return _presentation_parser(PythonPptxPresentationEngine(**settings))


def _json_factory(**settings: Any) -> HarborStructuredParser:
    return _structured_parser(JsonStructuredEngine(**settings))


def _text_factory(**settings: Any) -> HarborTextParser:
    return _text_parser(PlainTextEngine(**settings))


def _image_factory(**settings: Any) -> HarborImageParser:
    return HarborImageParser((OcrImageEngine(**settings),))


def _pdf_factory(
    *,
    backends: list[HarborPDFEngine] | None = None,
    min_content_chars: int = 20,
    max_pages: int | None = PDF_MAX_PAGES,
    profile: str = "liteparse",
    ocr_server_url: str | None = None,
) -> HarborPDFParser:
    if backends is not None:
        return HarborPDFParser(
            backends=backends,
            min_content_chars=min_content_chars,
            max_pages=max_pages,
            profile=profile,
        )
    router = PDFRouterConfig(default_profile=profile)
    return HarborParserFactory().create_pdf_parser(
        PDFParserConfig(
            min_content_chars=min_content_chars,
            max_pages=max_pages,
            router=router,
            ocr_server_url=ocr_server_url,
        )
    )


_PARSER_FACTORIES: Mapping[str, ParserFactory] = {
    "csv": _csv_factory,
    "docx": _docx_factory,
    "epub": _epub_factory,
    "excel": _excel_factory,
    "html": _html_factory,
    "image": _image_factory,
    "json": _json_factory,
    "markdown": _markdown_factory,
    "pdf": _pdf_factory,
    "pptx": _pptx_factory,
    "text": _text_factory,
}


@dataclass(frozen=True, slots=True)
class PdfBackendSpec:
    """Constructor metadata for one declaratively configurable PDF backend.

    Attributes:
        factory: Builds the engine from its typed options.
        option_factory: Dataclass the YAML settings are validated into, or
            ``None`` when the engine takes its settings as keywords.
        uses_chat_model: The engine reads pages with the chat model its
            ``model`` setting names, so it needs the runtime's chat client.
    """

    factory: PdfBackendFactory
    option_factory: OptionFactory | None = None
    uses_chat_model: bool = False

    def setting_names(self) -> set[str]:
        """Return YAML option names accepted by this backend."""
        if self.option_factory is None:
            return set()
        return {item.name for item in fields(self.option_factory)}  # type: ignore[arg-type]

    def build(
        self,
        settings: Mapping[str, Any],
        *,
        chat_client: HarborChatClientProtocol | None = None,
    ) -> HarborPDFEngine:
        """Construct a backend and its typed options from YAML settings.

        Args:
            settings: The backend's YAML settings, secrets already resolved.
            chat_client: Client a ``uses_chat_model`` backend sends pages through;
                other backends ignore it.

        Returns:
            The configured engine.

        Raises:
            ValueError: A chat-model backend was built without a chat client.
        """
        if self.option_factory is None:
            return self.factory(**settings)
        options = self.option_factory(**settings)
        if not self.uses_chat_model:
            return self.factory(options)
        if chat_client is None:
            raise ValueError(
                "it reads pages with a chat model from models.yaml, and no chat client "
                "was passed when the parsers were built"
            )
        return self.factory(options, chat_client=chat_client)


_PARSER_SETTING_NAMES: Mapping[str, frozenset[str]] = {
    **{name: frozenset() for name in _PARSER_FACTORIES if name not in {"image", "pdf"}},
    "image": frozenset({"config", "lang", "max_pixels", "ocr_engine", "ocr_server_url", "timeout"}),
    "pdf": frozenset({"max_pages", "min_content_chars", "ocr_server_url", "profile"}),
}
_PDF_BACKENDS: Mapping[str, PdfBackendSpec] = {
    "docling": PdfBackendSpec(DoclingPDFEngine, DoclingPDFConfig),
    "liteparse": PdfBackendSpec(LiteParsePDFEngine, LiteParsePDFConfig),
    "llm": PdfBackendSpec(LlmPDFEngine, LlmPDFConfig, uses_chat_model=True),
    "pymupdf": PdfBackendSpec(PyMuPDFEngine),
}

PDF_BACKEND_SECRET_FIELDS = frozenset({"password"})
PDF_BACKEND_PYTHON_ONLY_FIELDS = frozenset({"converter", "parser", "pipeline_options"})


def parser_factory(parser: str) -> ParserFactory | None:
    """Return a parser constructor by stable parser name."""
    return _PARSER_FACTORIES.get(parser)


def parser_setting_names(parser: str) -> frozenset[str]:
    """Return YAML settings accepted by a parser constructor."""
    return _PARSER_SETTING_NAMES.get(parser, frozenset())


def supported_parser_names() -> list[str]:
    """Return configurable parser names in stable display order."""
    return sorted(_PARSER_FACTORIES)


def pdf_backend_spec(backend: str) -> PdfBackendSpec | None:
    """Return constructor metadata for a PDF backend name."""
    return _PDF_BACKENDS.get(backend)


def supported_pdf_backend_names() -> list[str]:
    """Return configurable PDF backend names in deterministic order."""
    return sorted(_PDF_BACKENDS)
