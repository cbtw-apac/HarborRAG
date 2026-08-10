from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, ClassVar

from harborrag_adapters.parsers.errors import MaxFileSizeExceededError, ParseError
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.engines.docling.config import (
    DoclingPDFConfig,
)
from harborrag_adapters.parsers.pdf.engines.docling.utils import (
    DoclingConfigurationMixin,
)
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_adapters.parsers.pdf.normalization import (
    content_element,
    content_from_any,
)
from harborrag_adapters.parsers.pdf.resources import (
    materialized_pdf_path,
    raise_if_encrypted_pdf,
)
from harborrag_adapters.parsers.pdf.utils import merge_dataclass_options
from harborrag_core.domain.parser import ParseInput

DoclingBackendOptions = DoclingPDFConfig


class DoclingPDFEngine(DoclingConfigurationMixin, HarborPDFEngine):
    """Layout-aware PDF backend powered by IBM Docling."""

    name: ClassVar[str] = "docling"

    def __init__(
        self,
        options: DoclingPDFConfig | None = None,
        **overrides: Any,
    ) -> None:
        """Create a backend, accepting either an options object or overrides."""

        self.options = merge_dataclass_options(
            options,
            DoclingPDFConfig,
            overrides,
        )
        self._cached_converter: Any = None

    @property
    def supports_ocr(self) -> bool:
        return True

    @property
    def supports_tables(self) -> bool:
        return True

    @property
    def supports_layout(self) -> bool:
        return True

    def parse_input(self, input: ParseInput) -> PDFParseResult:
        """Convert a PDF through Docling and export the document content."""

        with materialized_pdf_path(input) as path:
            raise_if_encrypted_pdf(path)
            self._raise_if_exceeds_max_file_size(path)
            # Building a DocumentConverter loads hundreds of MB of layout/OCR
            # models, so it must not run until *after* the cheap encryption
            # and size checks above have had a chance to reject the file.
            converter = self._converter()
            try:
                result = converter.convert(path, **self._convert_kwargs())
            except Exception as exc:  # noqa: BLE001 - external parser boundary
                raise ParseError(f"Docling could not parse PDF: {exc}") from exc

        document = getattr(result, "document", result)
        content = self._export_document(document)
        metadata = self._metadata(result)
        if self.options.image_output_dir is not None:
            metadata["docling_image_paths"] = self._save_images(document)
        warnings = metadata.get("docling_errors") or []
        return PDFParseResult(
            content=content,
            engine=self.name,
            elements=content_element(self.name, content),
            metadata=metadata,
            warnings=warnings,
            raw=self._raw(document),
        )

    def _raise_if_exceeds_max_file_size(self, path: Path) -> None:
        """Reject an oversized file with a clear reason before Docling ever runs.

        Docling itself enforces ``max_file_size``, but only after
        ``convert()`` raises a ``ConversionError`` -- which the outer
        ``except Exception`` above wraps as "Docling could not parse PDF:
        ...", and the PDF router then wraps *that* again as "engine failed
        (...)". The real reason (a configured size policy, not a genuine
        parse failure) survives in the message but is buried three layers
        deep behind a headline ("No PDF engine produced acceptable content")
        that reads as a content/quality problem. Checking here raises the
        same fact as the router's top-level per-engine reason instead.
        """
        max_file_size = self.options.max_file_size
        if max_file_size is None:
            return
        size = path.stat().st_size
        if size > max_file_size:
            raise MaxFileSizeExceededError(size_bytes=size, max_bytes=max_file_size, engine=self.name)

    def _export_document(self, document: Any) -> str:
        """Export a Docling document using the configured output format."""

        export_format = self.options.export_format.lower().strip()
        if export_format == "doctags":
            return content_from_any(self._call_export(document, "export_to_doctags"))
        if export_format == "text":
            value = self._call_export(
                document,
                "export_to_markdown",
                strict_text=True,
            )
            if value is None:
                value = self._call_export(document, "export_to_text")
            return content_from_any(value)

        value = self._call_export(
            document,
            "export_to_markdown",
            strict_text=self.options.strict_text,
        )
        return content_from_any(value if value is not None else document)

    def _metadata(self, result: Any) -> dict[str, Any]:
        """Collect stable Docling metadata without exposing heavy document objects."""

        metadata: dict[str, Any] = {
            "source_engine": self.name,
            "docling_export_format": self.options.export_format,
            "docling_ocr_engine": self.options.ocr_engine,
            "docling_do_ocr": self.options.do_ocr,
            "docling_do_table_structure": self.options.do_table_structure,
        }

        status = getattr(result, "status", None)
        if status is not None:
            metadata["docling_status"] = getattr(status, "name", str(status))

        pages = getattr(result, "pages", None)
        if pages is not None:
            try:
                metadata["page_count"] = len(pages)
            except TypeError:
                metadata["page_count"] = None

        errors = getattr(result, "errors", None)
        if errors:
            metadata["docling_errors"] = [str(error) for error in errors]

        return metadata

    def _save_images(self, document: Any) -> list[str]:
        """Save page, picture, and table images to a fresh per-document subfolder.

        A per-document subfolder (rather than writing straight into
        ``image_output_dir``) is required: two documents converted against the
        same configured directory would otherwise overwrite each other's
        ``page-1.png``, ``picture-1.png``, etc.
        """

        image_output_dir = self.options.image_output_dir
        if image_output_dir is None:
            return []

        doc_dir = Path(image_output_dir) / uuid.uuid4().hex
        doc_dir.mkdir(parents=True, exist_ok=True)

        saved: list[str] = []
        for page_no, page in (getattr(document, "pages", None) or {}).items():
            self._save_pil_image(
                getattr(getattr(page, "image", None), "pil_image", None),
                doc_dir / f"page-{page_no}.png",
                saved,
            )
        for index, picture in enumerate(getattr(document, "pictures", None) or [], start=1):
            self._save_pil_image(
                self._element_image(picture, document),
                doc_dir / f"picture-{index}.png",
                saved,
            )
        for index, table in enumerate(getattr(document, "tables", None) or [], start=1):
            self._save_pil_image(
                self._element_image(table, document),
                doc_dir / f"table-{index}.png",
                saved,
            )
        return saved

    @staticmethod
    def _element_image(element: Any, document: Any) -> Any | None:
        """Render a picture/table element to a PIL image, tolerating API drift."""

        get_image = getattr(element, "get_image", None)
        if not callable(get_image):
            return None
        try:
            return get_image(document)
        except Exception:  # noqa: BLE001 - third-party config object boundary
            return None

    @staticmethod
    def _save_pil_image(pil_image: Any, path: Path, saved: list[str]) -> None:
        """Write a PIL image to disk and record its path, skipping missing images."""

        if pil_image is None:
            return
        try:
            pil_image.save(path)
        except Exception:  # noqa: BLE001 - third-party config object boundary
            return
        saved.append(str(path))

    def _raw(self, document: Any) -> dict[str, Any] | None:
        """Return a raw Docling payload only when explicitly requested."""

        if not self.options.include_raw_document:
            return None

        for method_name in ("export_to_dict", "model_dump", "dict"):
            value = self._call_export(document, method_name)
            if value is not None:
                return {"docling_document": value}
        return {"docling_document": content_from_any(document)}

    @staticmethod
    def _call_export(document: Any, method_name: str, **kwargs: Any) -> Any:
        """Call an export method and retry without kwargs for older Docling APIs."""

        method = getattr(document, method_name, None)
        if not callable(method):
            return None

        try:
            return method(**kwargs)
        except TypeError:
            return method()

    @staticmethod
    def _set_supported(target: Any, name: str, value: Any) -> bool:
        """Set a config value only when the target object supports that field."""

        if value is None:
            return False

        fields = getattr(target, "model_fields", None) or getattr(
            target,
            "__fields__",
            None,
        )
        if not hasattr(target, name) and (not fields or name not in fields):
            return False

        try:
            setattr(target, name, value)
            return True
        except Exception:  # noqa: BLE001 - third-party config object boundary
            return False


DoclingBackend = DoclingPDFEngine
