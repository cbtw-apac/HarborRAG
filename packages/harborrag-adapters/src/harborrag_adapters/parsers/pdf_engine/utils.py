from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput

from ..exceptions import EncryptedPdfError
from ..utils import compact_text

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def merge_dataclass_options[OptionT: "DataclassInstance"](
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
        # NEVER derive the on-disk name from the (attacker-influenced) filename:
        # "../../etc/cron.d/x.pdf" or an absolute path would escape the temp dir
        # and turn this into an arbitrary-file-write primitive. Nothing
        # downstream needs the original name, so use a fixed basename.
        path = Path(directory) / "document.pdf"
        path.write_bytes(input.read_bytes())
        yield path


def raise_if_encrypted_pdf(path: Path) -> None:
    """Fail fast on password-protected PDFs before running an expensive backend.

    Backends like Docling only report generic conversion failures, and their
    exact status/error vocabulary varies by version, so detection can't rely
    on inspecting a backend's own error output. PyMuPDF's ``needs_pass`` check
    is fast (header read, no full parse) and ships alongside every backend in
    the ``pdf`` extra, so it works as a reliable pre-check regardless of which
    backend runs first in a profile's fallback chain. Detection is
    best-effort: if PyMuPDF isn't importable, this silently no-ops and lets
    the calling backend's own error handling take over.
    """

    try:
        import pymupdf as pdf_module
    except ImportError:
        try:
            import fitz as pdf_module  # type: ignore[no-redef]
        except ImportError:
            return

    try:
        document = pdf_module.open(str(path), filetype="pdf")
    except Exception:  # noqa: BLE001 - best-effort pre-check boundary
        return

    try:
        if getattr(document, "needs_pass", False):
            raise EncryptedPdfError("PDF is password-protected")
    finally:
        document.close()


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


def content_from_any(
    value: Any,
    *,
    depth: int = 0,
    visited: set[int] | None = None,
) -> str:
    """Best-effort extraction of readable text from third-party result objects.

    ``depth`` and ``visited`` guard against adversarial/cyclic third-party
    result objects (a pathological deeply-nested or self-referential object
    could otherwise raise an uncaught ``RecursionError``), mirroring the
    depth cap and id-based visited set already used by ``_walk_text``. Both
    are keyword-only with defaults so existing call sites are unaffected.
    """

    if depth >= _MAX_WALK_DEPTH:
        return compact_text(str(value)) if isinstance(value, str) else ""

    if value is None:
        return ""
    if isinstance(value, str):
        return compact_text(value)
    if isinstance(value, bytes):
        return compact_text(value.decode("utf-8", errors="replace"))

    if visited is None:
        visited = set()
    if isinstance(value, (dict, list, tuple, set)):
        marker = id(value)
        if marker in visited:
            return ""
        visited.add(marker)

    def _recurse(child: Any) -> str:
        return content_from_any(child, depth=depth + 1, visited=visited)

    markdown = _call_or_value(value, "export_to_markdown")
    if markdown:
        return _recurse(markdown)

    text = _call_or_value(value, "export_to_text")
    if text:
        return _recurse(text)

    if isinstance(value, dict):
        for key in ("markdown", "md", "content", "text", "plain_text"):
            if key in value:
                content = _recurse(value[key])
                if content:
                    return content
        return compact_text("\n".join(_walk_text(value, depth + 1)))

    if isinstance(value, (list, tuple, set)):
        walked = compact_text("\n".join(_walk_text(value, depth + 1)))
        if walked:
            return walked
        return compact_text("\n".join(_recurse(item) for item in value))

    for attribute in ("markdown", "content", "text", "plain_text"):
        if hasattr(value, attribute):
            content = _recurse(getattr(value, attribute))
            if content:
                return content

    return compact_text(str(value))


def _call_or_value(value: Any, name: str) -> Any:
    """Return an attribute value, calling it when the attribute is a method."""

    attribute = getattr(value, name, None)
    return attribute() if callable(attribute) else attribute


_TEXT_KEYS = ("text", "content", "markdown", "md", "rec_text")
_MAX_WALK_DEPTH = 200


def _walk_text(
    value: Any,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Iterator[str]:
    """Recursively yield text-like fields from nested parser result structures.

    Guards against adversarial/cyclic third-party result objects (depth cap and
    an id-based visited set) and avoids emitting the same field twice by not
    re-walking the text keys already yielded from a dict.
    """

    if depth >= _MAX_WALK_DEPTH:
        return

    if isinstance(value, str):
        if value.strip():
            yield value
        return

    if seen is None:
        seen = set()
    if isinstance(value, (dict, list, tuple, set)):
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)

    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            child = value.get(key)
            if isinstance(child, str) and child.strip():
                yield child
        # Recurse only into the non-text-key children so matched strings above
        # are not yielded a second time.
        for key, child in value.items():
            if key in _TEXT_KEYS and isinstance(child, str):
                continue
            yield from _walk_text(child, depth + 1, seen)
        return

    if isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk_text(child, depth + 1, seen)
