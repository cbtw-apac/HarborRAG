from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput

from ..exceptions import ParseError
from ..utils import compact_text
from .base import PdfBackend, PdfParseResult
from .utils import (
    content_element,
    content_from_any,
    materialized_pdf_path,
    merge_dataclass_options,
)


@dataclass(slots=True)
class LiteParseBackendOptions:
    """Configuration for LlamaIndex LiteParse PDF extraction."""

    output_format: str = "markdown"
    image_mode: str = "placeholder"
    extract_links: bool = True
    ocr_enabled: bool = True
    ocr_language: str = "eng"
    ocr_server_url: str | None = None
    tessdata_path: Path | str | None = None
    max_pages: int | None = None
    target_pages: str | None = None
    dpi: int | float | None = None
    preserve_very_small_text: bool | None = None
    password: str | None = None
    quiet: bool | None = None
    num_workers: int | None = None
    input_mode: str = "path"
    include_raw: bool = False
    parser: Any | None = None
    extra_options: dict[str, Any] = field(default_factory=dict)


class LiteParseBackend(PdfBackend):
    """Adapter for LlamaIndex LiteParse.

    LiteParse is the local-first document parser from LlamaIndex. Its Python
    package exposes ``from liteparse import LiteParse`` and returns parsed text
    on ``result.text`` with per-page structured layout available on
    ``result.pages``.
    """

    name: ClassVar[str] = "liteparse"

    def __init__(
        self,
        options: LiteParseBackendOptions | None = None,
        **overrides: Any,
    ) -> None:
        """Create a LiteParse backend from options plus keyword overrides."""

        self.options = merge_dataclass_options(
            options,
            LiteParseBackendOptions,
            overrides,
        )
        self._cached_parser: Any = None

    def parse(self, input: ParseInput) -> PdfParseResult:
        """Run LiteParse and normalize its result into the PDF backend contract."""

        parser = self._parser()
        try:
            result = self._parse_with_liteparse(parser, input)
        except Exception as exc:  # noqa: BLE001 - external parser boundary
            raise ParseError(f"LiteParse could not parse PDF: {exc}") from exc

        content = self._content_from_result(result)
        return PdfParseResult(
            content=content,
            engine=self.name,
            elements=self._elements_from_result(result, content),
            metadata={
                "source_engine": self.name,
                "liteparse_output_format": self.options.output_format,
                "liteparse_ocr_enabled": self.options.ocr_enabled,
                "liteparse_target_pages": self.options.target_pages,
                "page_count": self._page_count(result),
            },
            raw=self._raw(result),
        )

    def _parser(self) -> Any:
        """Build or reuse the LiteParse parser instance."""

        if self.options.parser is not None:
            return self.options.parser
        if self._cached_parser is not None:
            return self._cached_parser

        try:
            from liteparse import LiteParse
        except ImportError as exc:
            raise ImportError(
                "PDF parsing with LiteParse requires the LlamaIndex `liteparse` "
                "package; install `harborrag-adapters[pdf]` or "
                "`pip install liteparse`."
            ) from exc

        # Reuse across documents: building LiteParse loads models and is too
        # costly to redo per document during bulk ingestion.
        self._cached_parser = LiteParse(**self._constructor_kwargs())
        return self._cached_parser

    def _constructor_kwargs(self) -> dict[str, Any]:
        """Translate backend options into LiteParse constructor keyword arguments."""

        values = {
            "output_format": self.options.output_format,
            "image_mode": self.options.image_mode,
            "extract_links": self.options.extract_links,
            "ocr_enabled": self.options.ocr_enabled,
            "ocr_language": self.options.ocr_language,
            "ocr_server_url": self.options.ocr_server_url,
            "tessdata_path": (
                str(self.options.tessdata_path) if self.options.tessdata_path is not None else None
            ),
            "max_pages": self.options.max_pages,
            "target_pages": self.options.target_pages,
            "dpi": self.options.dpi,
            "preserve_very_small_text": self.options.preserve_very_small_text,
            "password": self.options.password,
            "quiet": self.options.quiet,
            "num_workers": self.options.num_workers,
        }
        return {
            key: value
            for key, value in {**values, **self.options.extra_options}.items()
            if value is not None
        }

    def _parse_with_liteparse(self, parser: Any, input: ParseInput) -> Any:
        """Call LiteParse with either a materialized path or raw bytes."""

        parse = getattr(parser, "parse", None)
        if not callable(parse):
            raise ImportError("LiteParse parser does not expose `parse`.")

        input_mode = self.options.input_mode.lower().strip()
        if input_mode == "bytes":
            return parse(input.read_bytes())
        if input_mode != "path":
            raise ValueError("LiteParse input_mode must be `path` or `bytes`.")

        with materialized_pdf_path(input) as path:
            return parse(str(path))

    def _content_from_result(self, result: Any) -> str:
        """Read LiteParse's primary text field with a generic fallback.

        Only falls back to normalizing the whole ``result`` object when the
        ``text`` attribute is missing or ``None``; a present-but-empty string
        is a genuine empty extraction and must not be treated as "missing".
        """

        text = getattr(result, "text", None)
        if text is not None:
            return content_from_any(text)
        return content_from_any(result)

    def _elements_from_result(
        self,
        result: Any,
        content: str,
    ) -> list[DocumentElement]:
        """Prefer per-page LiteParse output, falling back to one content element."""

        elements = []
        for page_index, page in enumerate(getattr(result, "pages", []) or [], start=1):
            page_number = self._page_number(page, page_index)
            page_text = self._page_text(page)
            if not page_text:
                continue
            elements.append(
                DocumentElement(
                    id=f"pdf:{self.name}:page:{page_number}",
                    type="paragraph",
                    content=page_text,
                    metadata={"page": page_number, "engine": self.name},
                )
            )
        return elements or content_element(self.name, content)

    def _raw(self, result: Any) -> dict[str, Any] | None:
        """Expose the raw LiteParse result only when requested."""

        if not self.options.include_raw:
            return None

        for method_name in ("model_dump", "dict"):
            method = getattr(result, method_name, None)
            if callable(method):
                return {"liteparse_result": method()}
        return {"liteparse_result": content_from_any(result)}

    @staticmethod
    def _page_count(result: Any) -> int | None:
        """Return page count when LiteParse exposes a pages collection."""

        pages = getattr(result, "pages", None)
        if pages is None:
            return None
        try:
            return len(pages)
        except TypeError:
            return None

    @staticmethod
    def _page_number(page: Any, fallback: int) -> int:
        """Read page numbering from dict or object results."""

        if isinstance(page, dict):
            value = page.get("page_num") or page.get("page_number") or page.get("page")
        else:
            value = (
                getattr(page, "page_num", None)
                or getattr(page, "page_number", None)
                or getattr(page, "page", None)
            )
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _page_text(page: Any) -> str:
        """Extract page text from either a direct text field or text items."""

        if isinstance(page, dict):
            text = page.get("text")
            text_items = page.get("text_items") or page.get("textItems") or []
        else:
            text = getattr(page, "text", None)
            text_items = getattr(page, "text_items", None) or []
        if text:
            return compact_text(str(text))

        parts = []
        for item in text_items:
            item_text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if item_text:
                parts.append(str(item_text))
        return compact_text("\n".join(parts))
