from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar

from harborrag_core.domain.parser import ParsedDocument, ParseInput

from ..base import BaseParser
from ..exceptions import EncryptedPdfError, ParseError
from ..parser_logging import get_parser_logger, input_label, parser_log_extra
from .base import PdfBackend, PdfParseResult
from .docling import DoclingBackend, DoclingBackendOptions
from .liteparse import LiteParseBackend
from .mineru import MinerUBackend, MinerUBackendOptions
from .paddleocr import PaddleOcrBackend, PaddleOcrBackendOptions
from .pymupdf import PyMuPdfBackend

parser_logger = get_parser_logger("pdf")


class PdfParserProfile(StrEnum):
    """Predefined backend orderings for speed, OCR, and quality tradeoffs."""

    FAST = "fast"
    BALANCED = "balanced"
    OCR = "ocr"
    QUALITY = "quality"

    @classmethod
    def normalize(cls, value: PdfParserProfile | str) -> PdfParserProfile:
        """Coerce user configuration into a supported parser profile."""

        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower().strip())
        except ValueError as exc:
            supported = ", ".join(profile.value for profile in cls)
            raise ValueError(f"Unknown PDF parser profile {value!r}: {supported}") from exc


class PdfParser(BaseParser[ParseInput, ParsedDocument]):
    """Parse PDFs by trying configured backends until one returns usable text."""

    parser_name: ClassVar[str] = "pdf"
    parser_engine: ClassVar[str] = "pymupdf/docling/liteparse/mineru/paddleocr"
    suffixes: ClassVar[frozenset[str]] = frozenset({"pdf"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/pdf", "application/x-pdf"})

    def __init__(
        self,
        *,
        backends: Sequence[PdfBackend] | None = None,
        min_content_chars: int = 20,
        profile: PdfParserProfile | str = PdfParserProfile.BALANCED,
    ) -> None:
        """Configure the backend chain and minimum accepted text length."""

        self.profile = PdfParserProfile.normalize(profile)
        self.backends = (
            list(backends) if backends is not None else self.default_backends(self.profile)
        )
        self.min_content_chars = min_content_chars

    @staticmethod
    def default_backends(
        profile: PdfParserProfile | str = PdfParserProfile.BALANCED,
    ) -> list[PdfBackend]:
        """Build the default backend chain for a parsing profile."""

        profile = PdfParserProfile.normalize(profile)

        if profile == PdfParserProfile.FAST:
            return [PyMuPdfBackend(), LiteParseBackend()]

        if profile == PdfParserProfile.OCR:
            return [
                PyMuPdfBackend(),
                DoclingBackend(
                    DoclingBackendOptions(
                        force_full_page_ocr=True,
                        do_table_structure=True,
                    )
                ),
                MinerUBackend(MinerUBackendOptions(backend="pipeline")),
                PaddleOcrBackend(
                    PaddleOcrBackendOptions(
                        use_doc_orientation_classify=True,
                        use_doc_unwarping=True,
                        use_textline_orientation=True,
                        use_table_recognition=True,
                    )
                ),
            ]

        if profile == PdfParserProfile.QUALITY:
            return [
                DoclingBackend(
                    DoclingBackendOptions(
                        do_ocr=True,
                        do_table_structure=True,
                        table_do_cell_matching=True,
                        accelerator_device="auto",
                    )
                ),
                MinerUBackend(
                    MinerUBackendOptions(
                        backend="hybrid",
                        effort="medium",
                    )
                ),
                PaddleOcrBackend(
                    PaddleOcrBackendOptions(
                        use_doc_orientation_classify=True,
                        use_doc_unwarping=True,
                        use_textline_orientation=True,
                        use_table_recognition=True,
                        use_formula_recognition=True,
                        use_chart_recognition=True,
                        use_region_detection=True,
                    )
                ),
                PyMuPdfBackend(),
                LiteParseBackend(),
            ]

        # Previously this profile constructed two separate DoclingBackend
        # instances (one OCR-disabled for tables-only, one full-OCR
        # fallback). DoclingBackend lazily builds and memoizes its own
        # DocumentConverter (hundreds of MB of layout/OCR models) on first
        # use, so two instances meant two full model loads for the default
        # profile. PyMuPDF already covers the fast, non-OCR path earlier in
        # the chain, so a single Docling instance configured for OCR + table
        # structure covers both use cases the old pair was meant to serve.
        # Re-running the identical instance a second time later in the chain
        # would just repeat an already-failed call with the same input and
        # the same (failing) result, so the duplicate entry is dropped
        # rather than reused at two positions.
        return [
            PyMuPdfBackend(),
            DoclingBackend(
                DoclingBackendOptions(
                    do_ocr=True,
                    do_table_structure=True,
                )
            ),
            LiteParseBackend(),
            MinerUBackend(MinerUBackendOptions(backend="pipeline")),
            PaddleOcrBackend(PaddleOcrBackendOptions(use_table_recognition=True)),
        ]

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Run PDF backends in order and return the first sufficiently rich result."""

        parse_input = self.coerce_input(input)
        warnings: list[str] = []

        for backend in self.backends:
            parser_logger.debug(
                "Trying PDF backend %s for %s",
                backend.name,
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=backend.name,
                    profile=self.profile.value,
                ),
            )
            try:
                result = backend.parse(parse_input)
            except EncryptedPdfError:
                # No downstream engine can decrypt; stop the chain immediately
                # instead of burning OCR budget on every remaining backend.
                raise
            except ImportError as exc:
                warning = f"{backend.name}: unavailable ({exc})"
                warnings.append(warning)
                parser_logger.debug(
                    "Skipping unavailable PDF backend %s",
                    backend.name,
                    extra=parser_log_extra(
                        input=parse_input,
                        parser_name=self.parser_name,
                        parser_engine=backend.name,
                        profile=self.profile.value,
                    ),
                )
                continue
            except Exception as exc:  # noqa: BLE001 - normalize + continue chain
                # Any backend failure (ParseError or an unwrapped third-party
                # exception) must not abort the fallback chain; record and move
                # on to the next backend.
                warning = f"{backend.name}: failed ({exc})"
                warnings.append(warning)
                parser_logger.warning(
                    "PDF backend %s failed for %s: %s",
                    backend.name,
                    input_label(parse_input),
                    exc,
                    extra=parser_log_extra(
                        input=parse_input,
                        parser_name=self.parser_name,
                        parser_engine=backend.name,
                        profile=self.profile.value,
                    ),
                )
                continue

            warnings.extend(result.warnings)
            if result.has_content(self.min_content_chars):
                return self._document(parse_input, result, warnings)

            warning = f"{backend.name}: extracted less than {self.min_content_chars} characters"
            warnings.append(warning)
            parser_logger.debug(
                "PDF backend %s returned insufficient text for %s",
                backend.name,
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=backend.name,
                    profile=self.profile.value,
                    content_chars=len(result.content),
                ),
            )

        raise ParseError(
            "No PDF backend produced usable content. "
            f"Tried: {', '.join(backend.name for backend in self.backends)}. "
            f"Warnings: {'; '.join(warnings)}"
        )

    def _document(
        self,
        parse_input: ParseInput,
        result: PdfParseResult,
        warnings: list[str],
    ) -> ParsedDocument:
        """Convert a backend result into the shared ParsedDocument schema."""

        metadata = self.metadata_for(
            parse_input,
            pdf_engine=result.engine,
            pdf_profile=self.profile.value,
            **result.metadata,
        )
        return ParsedDocument(
            content=result.content,
            elements=result.elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=metadata,
            warnings=warnings or None,
            raw=result.raw,
        )
