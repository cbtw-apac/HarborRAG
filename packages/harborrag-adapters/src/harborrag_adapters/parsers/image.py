from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .base import BaseParser
from .exceptions import ParseError
from .parser_logging import get_parser_logger, input_label, parser_log_extra
from .utils import guard_input_size

parser_logger = get_parser_logger("image")

DEFAULT_MAX_IMAGE_PIXELS = 100_000_000  # 100 megapixels decoded


@dataclass(slots=True)
class ImageParser(BaseParser[ParseInput, ParsedDocument]):
    """Run OCR over raster images using pytesseract."""

    parser_name: ClassVar[str] = "image"
    parser_engine: ClassVar[str] = "pytesseract"
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

    lang: str | None = None
    config: str = ""
    timeout: int | float | None = 60
    max_pixels: int | None = DEFAULT_MAX_IMAGE_PIXELS

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Decode the image with Pillow, OCR it, and return extracted text."""

        parse_input = self.coerce_input(input)
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            parser_logger.error(
                "Image OCR dependencies are missing for %s",
                self.parser_name,
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError(
                "Image OCR requires `pytesseract` and `Pillow`; install "
                "`harborrag-adapters[parsers]` or `pip install pytesseract Pillow`."
            ) from exc

        try:
            parser_logger.debug(
                "Running OCR for %s",
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                    ocr_lang=self.lang,
                ),
            )
            data = guard_input_size(parse_input.read_bytes())
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
                content = pytesseract.image_to_string(
                    image,
                    lang=self.lang,
                    config=self.config,
                    timeout=self.timeout,
                ).strip()
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
                    parser_engine=self.parser_engine,
                    ocr_lang=self.lang,
                ),
            )
            raise ParseError(f"Image OCR failed: {exc}") from exc

        elements = [
            DocumentElement(
                id="image:ocr:0",
                type="image",
                content=content,
                metadata={"filename": parse_input.filename},
            )
        ] if content else []
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, lang=self.lang),
        )
