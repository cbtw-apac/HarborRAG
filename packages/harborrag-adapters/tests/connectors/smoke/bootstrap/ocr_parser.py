"""RapidOCR image parsing wired into the smoke-script parser stack."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, ClassVar

from harborrag_adapters.parsers.common.base import BaseParser
from harborrag_adapters.parsers.image.parser import HarborImageParser
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .catalogs import parser_catalog
from .ocr_server import apply_host_ocr_server_url

if TYPE_CHECKING:
    from harborrag_adapters.connectors.attachments.processing import CustomAttachmentParser
    from harborrag_adapters.parsers import HarborParserRegistry
    from harborrag_runtime.config import ParserCatalog


class RapidOcrImageParser(BaseParser[ParseInput, ParsedDocument]):
    """Route image OCR through RapidOCR instead of the default pytesseract parser.

    Subclassing `BaseParser` (rather than duck-typing) matters here: its
    `__init_subclass__` normalizes `suffixes` to the dot-prefixed form
    `HarborParser`'s suffix routing actually indexes on (`"png"` -> `".png"`).
    A plain class with dot-less suffixes silently never matches by suffix —
    it would only ever route by `content_type`, which local files don't set.
    """

    parser_name: ClassVar[str] = "image"
    parser_engine: ClassVar[str] = "rapidocr"
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

    def parse(self, input: ParseInput) -> ParsedDocument:
        parse_input = self.coerce_input(input)
        from harborrag_adapters.parsers.common.resources import (
            parse_input_suffix,
            read_parse_input_bytes,
        )

        try:
            text = _parse_image_with_rapidocr(
                read_parse_input_bytes(parse_input),
                parse_input_suffix(parse_input),
            )
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} (file={parse_input.filename!r})") from exc
        elements = (
            [
                DocumentElement(
                    id="image:ocr:0",
                    type="image",
                    content=text,
                    metadata={"filename": parse_input.filename},
                )
            ]
            if text
            else []
        )
        return ParsedDocument(
            content=text,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input),
        )


def build_harbor_parser() -> HarborParserRegistry:
    """Assemble the parser stack from `config/parsers.yaml`.

    Both the PDF chain and the image engine are whatever the catalog enables
    (LiteParse against the OCR server in the shipped configuration), so a
    smoke run exercises the same engines the worker does. The OCR server URL
    is retargeted for this host afterwards, because the shipped value names a
    container-only DNS alias.
    """
    catalog = parser_catalog()
    harbor_parser = catalog.build_harbor_parser(environment=os.environ)
    apply_host_ocr_server_url(harbor_parser)
    _print_engine_selection(harbor_parser, _reported_families(catalog))
    return harbor_parser


def _reported_families(catalog: ParserCatalog) -> frozenset[str]:
    """Parser families whose engine choice this run should report.

    Only the configured families (plus the image family the bootstrap wires
    itself) are named: the untouched defaults for text, markup, and the rest
    carry no configuration decision worth echoing.
    """
    return frozenset(
        {catalog.get(name).parser for name in catalog.names(enabled_only=True)}
        | {HarborImageParser.parser_name}
    )


def _print_engine_selection(
    harbor_parser: HarborParserRegistry,
    families: frozenset[str],
) -> None:
    """Name the engines that will actually parse, per configured parser family.

    Which PDF engine runs is a configuration decision, so a smoke run that
    reports real parsing must say which engine it exercised instead of
    leaving it to be inferred from the checked-in catalog.
    """
    for family in harbor_parser.families():
        if family.parser_name not in families:
            continue
        engines = [
            # `name` is the concrete selection (e.g. "liteparse"); the class-level
            # `parser_engine` only names the family's supported providers.
            str(getattr(engine, "name", None) or getattr(engine, "parser_engine", ""))
            for engine in getattr(family, "engines", ())
        ]
        if engines:
            print(f"[parsers] {family.parser_name} engines={', '.join(engines)}")


def attachment_custom_parsers() -> dict[Any, CustomAttachmentParser]:
    """Take no attachment type away from the configured parser registry.

    Image attachments used to be diverted to RapidOCR here. The catalog now
    configures the image family itself, so leaving this empty lets every
    attachment route through the same engines `config/parsers.yaml` selects.
    """
    return {}


_RAPID_OCR_ENGINE: Any | None = None


def _rapidocr_engine():
    """Build one RapidOCR engine and reuse its loaded ONNX models."""
    global _RAPID_OCR_ENGINE
    if _RAPID_OCR_ENGINE is None:
        try:
            import onnxruntime
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                "RapidOCR image parsing requires `harborrag-adapters[pdf]`, "
                "which installs both `rapidocr` and `onnxruntime`"
            ) from exc
        _RAPID_OCR_ENGINE = RapidOCR()
        providers = onnxruntime.get_available_providers()
        print(
            "[attachments] RapidOCR runtime='onnxruntime' "
            f"provider='CPUExecutionProvider' available_providers={providers!r}"
        )
    return _RAPID_OCR_ENGINE


def _prepare_rapidocr_bytes(content: bytes, extension: str) -> bytes:
    """RapidOCR is sensitive to CMYK and similar non-RGB modes; convert them
    to RGB before handing the payload to the ONNX detector so scans and print
    images do not fail with an empty detection result."""
    _ = extension
    if not content:
        return content

    try:
        from PIL import Image
    except ImportError:
        return content

    try:
        image = Image.open(__import__("io").BytesIO(content))
        mode = getattr(image, "mode", "")
        if mode.upper() in {"CMYK", "YCBCR", "YCbCr", "RGBA", "LA", "P"}:
            rgb_image = image.convert("RGB")
            buffer = __import__("io").BytesIO()
            rgb_image.save(buffer, format="PNG")
            return buffer.getvalue()
    except (Image.DecompressionBombError, Image.UnidentifiedImageError, OSError, ValueError):
        return content

    return content


def _parse_image_with_rapidocr(content: bytes, extension: str) -> str:
    """Extract ordered text lines from one image with RapidOCR."""
    if not content:
        return ""

    content = _prepare_rapidocr_bytes(content, extension)

    try:
        result = _rapidocr_engine()(content)
    except Exception as exc:
        # RapidOCR decodes internally via its own unguarded `Image.open()`,
        # whose failure carries a raw `<_io.BytesIO object at 0x...>` repr
        # with no detail about the file -- classify it instead of letting it
        # escape as-is. Import lazily: this only runs on a decode failure,
        # so tests that mock `_rapidocr_engine` never reach this branch.
        from PIL import Image

        if isinstance(exc, Image.DecompressionBombError):
            raise RuntimeError(
                f"Image OCR failed ({len(content)} bytes): exceeds Pillow's "
                f"decompression-bomb pixel limit: {exc}"
            ) from exc
        if isinstance(exc, Image.UnidentifiedImageError):
            raise RuntimeError(
                f"Image OCR failed ({len(content)} bytes): cannot decode as a supported "
                "image format; the file may be corrupt, truncated, or not actually an image."
            ) from exc
        raise

    texts = getattr(result, "txts", None) or ()
    return "\n".join(str(text).strip() for text in texts if str(text).strip())
