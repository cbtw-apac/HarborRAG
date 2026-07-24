from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .archive_safety import guard_input_size
from .base import BaseParser
from .exceptions import ParseError
from .input_loading import read_parse_input_bytes
from .parser_logging import get_parser_logger, input_label, parser_log_extra

parser_logger = get_parser_logger("image")

DEFAULT_MAX_IMAGE_PIXELS = 100_000_000  # 100 megapixels decoded


@dataclass(slots=True)
class ImageParser(BaseParser[ParseInput, ParsedDocument]):
    """Run OCR over raster images using a selectable local OCR engine."""

    parser_name: ClassVar[str] = "image"
    parser_engine: ClassVar[str] = "rapidocr/pytesseract"
    suffixes: ClassVar[frozenset[str]] = frozenset(
        {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp"}
    )
    content_types: ClassVar[frozenset[str]] = frozenset(
        {
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/tiff",
            "image/bmp",
            "image/gif",
            "image/webp",
        }
    )

    ocr_engine: str = "pytesseract"
    lang: str | None = None
    config: str = ""
    timeout: int | float | None = 60
    max_pixels: int | None = DEFAULT_MAX_IMAGE_PIXELS
    _rapidocr_engine: Any | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Normalize and validate the configured OCR engine."""

        if not isinstance(self.ocr_engine, str):
            raise ValueError("Image OCR engine must be a string")
        self.ocr_engine = self.ocr_engine.lower().strip()
        if self.ocr_engine not in {"pytesseract", "rapidocr"}:
            raise ValueError(
                f"Unsupported image OCR engine {self.ocr_engine!r}: "
                "pytesseract, rapidocr"
            )
        if self.max_pixels is not None and (
            not isinstance(self.max_pixels, int)
            or isinstance(self.max_pixels, bool)
            or self.max_pixels <= 0
        ):
            raise ValueError("Image max_pixels must be a positive integer or null")

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Decode the image with Pillow, OCR it, and return extracted text."""

        parse_input = self.coerce_input(input)
        try:
            from PIL import Image
        except ImportError as exc:
            parser_logger.error(
                "Image OCR dependencies are missing for %s",
                self.ocr_engine,
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.ocr_engine,
                ),
            )
            raise ParseError(
                "Image OCR requires `Pillow`; install "
                "`harborrag-adapters[parsers]` or `pip install Pillow`."
            ) from exc

        try:
            parser_logger.debug(
                "Running OCR for %s",
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.ocr_engine,
                    ocr_lang=self.lang,
                ),
            )
            data = guard_input_size(read_parse_input_bytes(parse_input))
            with Image.open(BytesIO(data)) as image:
                # Pillow's `open()` only reads the header, so the encoded size
                # guard above doesn't bound the decoded pixel buffer. Check the
                # dimensions before `.load()` actually decodes the image, since
                # a small, highly-compressed file can still expand to an
                # enormous in-memory bitmap.
                width, height = image.size
                pixel_count = width * height
                if self.max_pixels is not None and pixel_count > self.max_pixels:
                    raise ParseError(
                        f"Image {width}x{height} ({pixel_count} pixels) exceeds "
                        f"max_pixels {self.max_pixels}"
                    )
                image.load()
                content = self._extract_text(data, image)
        except ParseError:
            raise
        except (RuntimeError, OSError, ValueError) as exc:
            parser_logger.warning(
                "Image OCR failed for %s: %s",
                input_label(parse_input),
                exc,
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.ocr_engine,
                    ocr_lang=self.lang,
                ),
            )
            raise ParseError(f"Image OCR failed: {exc}") from exc

        elements = (
            [
                DocumentElement(
                    id="image:ocr:0",
                    type="image",
                    content=content,
                    metadata={"filename": parse_input.filename},
                )
            ]
            if content
            else []
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(
                parse_input,
                ocr_engine=self.ocr_engine,
                lang=self.lang,
            ),
        )

    def _extract_text(self, data: bytes, image: Any) -> str:
        """Dispatch OCR while keeping optional dependencies lazy."""

        if self.ocr_engine == "rapidocr":
            return self._extract_with_rapidocr(data)
        return self._extract_with_pytesseract(image)

    def _extract_with_pytesseract(self, image: Any) -> str:
        """Extract text with the legacy Tesseract adapter."""

        try:
            import pytesseract
        except ImportError as exc:
            raise ParseError(
                "Image OCR with pytesseract requires `pytesseract`; install "
                "`harborrag-adapters[parsers]` or `pip install pytesseract`."
            ) from exc

        content = pytesseract.image_to_string(
            image,
            lang=self.lang,
            config=self.config,
            timeout=self.timeout,
        )
        return str(content).strip()

    def _extract_with_rapidocr(self, data: bytes) -> str:
        """Extract ordered text lines with a memoized RapidOCR engine."""

        if self._rapidocr_engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise ParseError(
                    "Image OCR with RapidOCR requires `rapidocr` and an inference "
                    "runtime; install `harborrag-adapters[pdf]`."
                ) from exc
            self._rapidocr_engine = RapidOCR()

        result = self._rapidocr_engine(data)
        texts = getattr(result, "txts", None) or ()
        return "\n".join(
            text for value in texts if (text := str(value).strip())
        )
