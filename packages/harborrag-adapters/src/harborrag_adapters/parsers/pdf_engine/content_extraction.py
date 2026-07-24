from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from harborrag_core.domain.element import DocumentElement

from ..text_extraction import compact_text


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
