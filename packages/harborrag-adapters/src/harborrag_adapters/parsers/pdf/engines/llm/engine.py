"""PDF engine that has a multimodal chat model transcribe each page to Markdown.

Pages are rendered with PyMuPDF and sent as images through the core chat-client
port, so the model, its provider and its API key all come from models.yaml and
nothing here names a provider. Page images leave the process, so this engine
runs only where a parser definition lists it in an explicit engines chain.

Every error it raises carries page numbers and the model name, never page text.
A retryable provider failure is a ``HarborUnavailableError``, which the PDF
chain re-raises so the activity retries; any other model failure is a
``ParseError``, which lets the chain try its next engine.

Start from: LlmPDFEngine.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import ClassVar

from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.validation import guard_input_size
from harborrag_adapters.parsers.errors import (
    EncryptedPdfError,
    MaxPagesExceededError,
    NoExtractableTextError,
    ParseError,
)
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.engines.llm.config import LlmPDFConfig
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_adapters.parsers.pdf.normalization import page_element
from harborrag_adapters.parsers.pdf.utils import import_pymupdf
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.parser import ParseInput
from harborrag_core.models.chat import (
    FinishReason,
    HarborChatMessage,
    ImageURL,
    ImageURLContentPart,
    MessageRole,
    TextContentPart,
)
from harborrag_core.models.errors import HarborChatError
from harborrag_core.ports.model_clients import HarborChatClientProtocol

__all__ = ["LlmPDFEngine"]

_PROMPT = (
    "Transcribe this PDF page into Markdown. Keep the reading order, headings, "
    "lists and tables, writing tables as Markdown tables. Describe each chart or "
    "figure in one sentence in square brackets. Output only the page content, "
    "with no commentary and no code fences. Output nothing for a blank page."
)
# 10 MB of PNG is about 13.4M base64 characters, inside the 20M-character
# ceiling core puts on one image data URL.
_MAX_PAGE_IMAGE_BYTES = 10_000_000
_FENCED = re.compile(r"\A```[\w-]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)


@dataclass(frozen=True, slots=True)
class _PageTranscript:
    number: int
    text: str
    warning: str | None = None


class LlmPDFEngine(HarborPDFEngine):
    """HarborPDFEngine that reads scans, tables and layout through a vision chat model.

    One chat call per page, sent one after another, at temperature 0 and marked
    sensitive so the model cache never stores page images.

    Attributes:
        name: Engine name used in ``engines:`` chains and parse warnings.

    Args:
        options: Chat model and page limits.
        chat_client: Synchronous client the runtime built from models.yaml. The
            engine calls it from the parser's worker thread and never closes it.
    """

    name: ClassVar[str] = "llm"

    def __init__(self, options: LlmPDFConfig, *, chat_client: HarborChatClientProtocol) -> None:
        self.options = options
        self._chat_client = chat_client

    @property
    def supports_ocr(self) -> bool:
        """Report that scanned pages are read, since the model sees the page image."""
        return True

    @property
    def supports_tables(self) -> bool:
        """Report that tables come back as Markdown tables."""
        return True

    @property
    def supports_layout(self) -> bool:
        """Report that headings and reading order are kept."""
        return True

    def parse_input(self, input: ParseInput) -> PDFParseResult:
        """Render every page and return the model's Markdown, one section per page.

        Args:
            input: The PDF bytes or path.

        Returns:
            Page sections in order, with one element per non-blank page.

        Raises:
            ParseError: The PDF can't be opened, a page image is too large, or the
                model failed or refused a page.
            EncryptedPdfError: The PDF needs a password.
            MaxPagesExceededError: The PDF has more pages than ``max_pages``.
            NoExtractableTextError: The model returned nothing for every page.
        """
        pymupdf = import_pymupdf()
        source_bytes = guard_input_size(read_parse_input_bytes(input))
        try:
            document = pymupdf.open(stream=source_bytes, filetype="pdf")
        except (RuntimeError, ValueError) as exc:
            raise ParseError(f"{self.name} engine could not open the PDF") from exc

        try:
            if document.needs_pass:
                raise EncryptedPdfError("PDF is password-protected")
            page_count = document.page_count
            if page_count > self.options.max_pages:
                raise MaxPagesExceededError(
                    page_count=page_count,
                    max_pages=self.options.max_pages,
                    engine=self.name,
                )
            transcripts = [
                self._transcribe(page.get_pixmap(dpi=self.options.dpi).tobytes("png"), number)
                for number, page in enumerate(document, start=1)
            ]
        finally:
            document.close()

        written = [transcript for transcript in transcripts if transcript.text]
        if page_count > 0 and not written:
            raise NoExtractableTextError(page_count=page_count)
        return PDFParseResult(
            content="\n\n".join(f"Page {page.number}\n{page.text}" for page in written),
            engine=self.name,
            elements=[page_element(self.name, page.number, page.text) for page in written],
            metadata={"page_count": page_count},
            warnings=[page.warning for page in transcripts if page.warning],
        )

    def _transcribe(self, image: bytes, number: int) -> _PageTranscript:
        """Send one rendered page to the model and return its Markdown."""
        if len(image) > _MAX_PAGE_IMAGE_BYTES:
            raise ParseError(
                f"page {number} renders to {len(image)} bytes at {self.options.dpi} dpi, "
                f"over the {_MAX_PAGE_IMAGE_BYTES}-byte limit; lower the engine's dpi"
            )
        message = HarborChatMessage(
            role=MessageRole.USER,
            content=(
                TextContentPart(text=_PROMPT),
                ImageURLContentPart(image_url=ImageURL(url=_png_data_url(image))),
            ),
        )
        try:
            response = self._chat_client.chat(
                [message], model=self.options.model, temperature=0, sensitive=True
            )
        except HarborChatError as exc:
            reason = f"model {self.options.model!r} failed on page {number} ({type(exc).__name__})"
            if exc.retryable:
                raise HarborUnavailableError(reason) from exc
            raise ParseError(reason) from exc

        if response.finish_reason is FinishReason.CONTENT_FILTER:
            raise ParseError(f"model {self.options.model!r} refused page {number}")
        warning = None
        if response.finish_reason is FinishReason.LENGTH:
            warning = f"page {number}: transcription stopped at the model's output limit"
        return _PageTranscript(number, _unfenced(response.text), warning)


def _png_data_url(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


def _unfenced(text: str) -> str:
    """Drop one code fence wrapped around the whole reply, which some models add anyway."""
    stripped = text.strip()
    fenced = _FENCED.match(stripped)
    return fenced.group("body").strip() if fenced else stripped
