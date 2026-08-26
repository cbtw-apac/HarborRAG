from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, ClassVar

from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import guard_input_size
from harborrag_adapters.parsers.errors import (
    MaxPixelsExceededError,
    ParseError,
    UnreadableImageError,
)
from harborrag_adapters.parsers.image.base import HarborImageEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("image")

DEFAULT_MAX_IMAGE_PIXELS = 100_000_000  # 100 megapixels decoded


@dataclass(slots=True)
class OcrImageEngine(HarborImageEngine):
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
                f"Unsupported image OCR engine {self.ocr_engine!r}: pytesseract, rapidocr"
            )
        if self.max_pixels is not None and (
            not isinstance(self.max_pixels, int)
            or isinstance(self.max_pixels, bool)
            or self.max_pixels <= 0
        ):
            raise ValueError("Image max_pixels must be a positive integer or null")

    @property
    def name(self) -> str:
        return self.ocr_engine

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
                "`harborrag-adapters[image]` or `pip install Pillow`."
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
            if not data:
                # 0 bytes has no image format for Pillow to identify, so
                # `Image.open()` would otherwise reject it as unreadable.
                # There is nothing to OCR, so succeed with empty output like
                # the other engines.
                return self.empty_result(
                    parse_input,
                    ocr_engine=self.ocr_engine,
                    lang=self.lang,
                )
            with Image.open(BytesIO(data)) as image:
                # Pillow's `open()` only reads the header, so the encoded size
                # guard above doesn't bound the decoded pixel buffer. Check the
                # dimensions before `.load()` actually decodes the image, since
                # a small, highly-compressed file can still expand to an
                # enormous in-memory bitmap.
                width, height = image.size
                pixel_count = width * height
                if self.max_pixels is not None and pixel_count > self.max_pixels:
                    raise MaxPixelsExceededError(
                        width=width,
                        height=height,
                        max_pixels=self.max_pixels,
                        filename=parse_input.filename,
                    )
                image.load()
                # OCR providers use several no-detection sentinels across
                # versions (``None``, an empty result object, or whitespace).
                # They all mean a successful parse with no extracted text.
                content = (self._extract_text(data, image) or "").strip()
        except ParseError:
            raise
        except Image.UnidentifiedImageError as exc:
            # Pillow's own message is a raw `<_io.BytesIO object at 0x...>`
            # repr with no detail about the file, so it gets replaced (not
            # wrapped) with one naming the file and its size.
            parser_logger.warning(
                "Image OCR failed for %s: unreadable image (%d bytes)",
                input_label(parse_input),
                len(data),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.ocr_engine,
                    ocr_lang=self.lang,
                ),
            )
            raise UnreadableImageError(filename=parse_input.filename, size_bytes=len(data)) from exc
        except (RuntimeError, OSError, ValueError, Image.DecompressionBombError) as exc:
            # `Image.open()` runs its own pixel-count guard against Pillow's
            # global `MAX_IMAGE_PIXELS` *before* our `max_pixels` check gets a
            # chance to run (headers alone can push a file more than 2x over
            # Pillow's default threshold). `DecompressionBombError` subclasses
            # `Exception` directly, not `OSError`, so it must be listed
            # explicitly or it escapes as an uncaught crash instead of the
            # typed `ParseError` below.
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
        parser_logger.info(
            "Parsed image OCR %s engine=%s content_chars=%d elements=%d",
            input_label(parse_input),
            self.ocr_engine,
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.ocr_engine,
                ocr_lang=self.lang,
                content_chars=len(content),
                elements=len(elements),
            ),
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
            data = self._prepare_rapidocr_bytes(data, image)
            return self._extract_with_rapidocr(data)
        return self._extract_with_pytesseract(image)

    def _prepare_rapidocr_bytes(self, data: bytes, image: Any) -> bytes:
        """RapidOCR is sensitive to CMYK and similar non-RGB modes; convert to
        RGB before handing the payload to the ONNX detector so scans and print
        images do not fail with a misleading empty detection result."""
        mode = getattr(image, "mode", "")
        if mode.upper() in {"CMYK", "YCBCR", "YCbCr", "RGBA", "LA", "P"}:
            rgb_image = image.convert("RGB")
            buffer = BytesIO()
            rgb_image.save(buffer, format="PNG")
            return buffer.getvalue()
        return data

    def _extract_with_pytesseract(self, image: Any) -> str:
        """Extract text with the legacy Tesseract adapter."""

        try:
            import pytesseract
        except ImportError as exc:
            raise ParseError(
                "Image OCR with pytesseract requires `pytesseract`; install "
                "`harborrag-adapters[image-tesseract]` or `pip install pytesseract`."
            ) from exc

        try:
            content = pytesseract.image_to_string(
                image,
                lang=self.lang,
                config=self.config,
                timeout=self.timeout,
            )
        except pytesseract.TesseractError as exc:
            # Tesseract exits non-zero both for genuine failures (missing
            # language data, corrupt image) and for the documented "no text
            # detected" case, which it signals with this specific message.
            # Only that signal means a successful parse with empty output;
            # every other TesseractError still surfaces as a failure.
            if "empty page" in str(exc).lower():
                return ""
            raise
        return "" if content is None else str(content).strip()

    def _extract_with_rapidocr(self, data: bytes) -> str:
        """Extract ordered text lines with a memoized RapidOCR engine."""

        if self._rapidocr_engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise ParseError(
                    "Image OCR with RapidOCR requires `rapidocr` and an inference "
                    "runtime; install `harborrag-adapters[image-rapidocr]`."
                ) from exc
            self._rapidocr_engine = RapidOCR()

        result = self._rapidocr_engine(data)
        texts = getattr(result, "txts", None) or ()
        return "\n".join(text for value in texts if (text := str(value).strip()))


ImageParser = OcrImageEngine
