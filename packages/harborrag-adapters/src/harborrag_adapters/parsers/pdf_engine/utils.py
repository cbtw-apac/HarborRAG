from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypeVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput

from ..utils import compact_text


OptionT = TypeVar("OptionT")


def merge_dataclass_options(
    options: OptionT | None,
    option_type: type[OptionT],
    overrides: dict[str, Any],
) -> OptionT:
    """Merge explicit option objects with keyword overrides for backend setup."""

    merged = options or option_type()
    if not overrides:
        return merged

    supported = {field.name for field in fields(merged)}
    unknown = sorted(set(overrides) - supported)
    if unknown:
        names = ", ".join(unknown)
        raise TypeError(f"Unsupported option(s) for {option_type.__name__}: {names}")
    return replace(merged, **overrides)


@contextmanager
def materialized_pdf_path(input: ParseInput) -> Iterator[Path]:
    """Yield a real PDF path for engines that cannot parse in-memory bytes."""

    if input.path is not None:
        yield Path(input.path)
        return

    with TemporaryDirectory(prefix="harborrag-pdf-") as directory:
        filename = input.filename or "document.pdf"
        path = Path(directory) / filename
        if path.suffix.lower() != ".pdf":
            path = path.with_suffix(".pdf")
        path.write_bytes(input.read_bytes())
        yield path


def page_element(engine: str, page: int, content: str) -> DocumentElement:
    """Build a standard page-level element for PDF backends."""

    return DocumentElement(
        id=f"pdf:{engine}:page:{page}",
        type="paragraph",
        content=compact_text(content),
        metadata={"page": page, "engine": engine},
    )


def content_element(engine: str, content: str) -> list[DocumentElement]:
    """Build a single fallback content element when page structure is unavailable."""

    content = compact_text(content)
    if not content:
        return []
    return [
        DocumentElement(
            id=f"pdf:{engine}:0",
            type="paragraph",
            content=content,
            metadata={"engine": engine},
        )
    ]


def content_from_any(value: Any) -> str:
    """Best-effort extraction of readable text from third-party result objects."""

    if value is None:
        return ""
    if isinstance(value, str):
        return compact_text(value)
    if isinstance(value, bytes):
        return compact_text(value.decode("utf-8", errors="replace"))

    markdown = _call_or_value(value, "export_to_markdown")
    if markdown:
        return content_from_any(markdown)

    text = _call_or_value(value, "export_to_text")
    if text:
        return content_from_any(text)

    if isinstance(value, dict):
        for key in ("markdown", "md", "content", "text", "plain_text"):
            if key in value:
                content = content_from_any(value[key])
                if content:
                    return content
        return compact_text("\n".join(_walk_text(value)))

    if isinstance(value, (list, tuple, set)):
        walked = compact_text("\n".join(_walk_text(value)))
        if walked:
            return walked
        return compact_text("\n".join(content_from_any(item) for item in value))

    for attribute in ("markdown", "content", "text", "plain_text"):
        if hasattr(value, attribute):
            content = content_from_any(getattr(value, attribute))
            if content:
                return content

    return compact_text(str(value))


def _call_or_value(value: Any, name: str) -> Any:
    """Return an attribute value, calling it when the attribute is a method."""

    attribute = getattr(value, name, None)
    return attribute() if callable(attribute) else attribute


def _walk_text(value: Any) -> Iterator[str]:
    """Recursively yield text-like fields from nested parser result structures."""

    if isinstance(value, str):
        if value.strip():
            yield value
        return

    if isinstance(value, dict):
        for key in ("text", "content", "markdown", "md", "rec_text"):
            child = value.get(key)
            if isinstance(child, str) and child.strip():
                yield child
        for child in value.values():
            yield from _walk_text(child)
        return

    if isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk_text(child)
