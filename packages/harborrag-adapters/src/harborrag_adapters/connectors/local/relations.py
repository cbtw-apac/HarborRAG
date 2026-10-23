"""Markdown and HTML link extraction for local files as ``links_to`` relations."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Label and target classes exclude the brackets, so a scan stops at the next "["
# and every character is examined a bounded number of times.
_MARKDOWN_LINK = re.compile(r"""!?\[[^\[\]]*\]\(([^()\s\[\]]+)(?:\s+(?:"[^"\n]*"|'[^'\n]*'))?\)""")
_HTML_LINK = re.compile(
    r"""(?:href|src)\s*=\s*["']([^"']+)["']""",
    flags=re.IGNORECASE,
)
_IGNORED_SCHEMES = frozenset({"data", "http", "https", "mailto", "tel", "javascript"})


class LocalDocumentRelationResolver:
    """Resolver that turns links in local documents into relations to files beneath one root."""

    def __init__(self, root_path: Path) -> None:
        """Pin the resolved trust root.

        Args:
            root_path: Directory that link targets must be beneath.
        """
        self._root_path = root_path.resolve()

    def relations(
        self,
        *,
        source_path: Path,
        content: bytes,
        media_type: str,
    ) -> list[dict[str, object]]:
        """Return ``links_to`` relations for links that resolve to files under the root.

        Targets are root-relative POSIX paths, deduplicated and sorted.

        Args:
            source_path: Path of the linking file.
            content: Bytes of the linking file; non-text media yield no relations.
            media_type: MIME type of the linking file.
        """
        if not self._is_text(media_type, source_path):
            return []
        text = content.decode("utf-8", errors="replace")
        targets: dict[str, str] = {}
        for raw_target in (*_MARKDOWN_LINK.findall(text), *_HTML_LINK.findall(text)):
            resolved = self._resolve(source_path, raw_target)
            if resolved is not None:
                targets[resolved.relative_to(self._root_path).as_posix()] = raw_target
        return [
            {
                "predicate": "links_to",
                "target_id": target_id,
                "target_type": "document",
                "metadata": {"source_link": targets[target_id]},
            }
            for target_id in sorted(targets)
        ]

    def _resolve(
        self,
        source_path: Path,
        raw_target: str,
    ) -> Path | None:
        """Return the file a link points to, or ``None`` if external, missing or out of scope."""
        target = unquote(raw_target.strip().strip("<>"))
        if not target or target.startswith("#"):
            return None
        parsed = urlsplit(target)
        if parsed.scheme.casefold() in _IGNORED_SCHEMES or parsed.netloc:
            return None
        path_value = parsed.path
        if not path_value:
            return None
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = source_path.parent / candidate
        try:
            resolved = candidate.resolve(strict=False)
            if not resolved.is_relative_to(self._root_path):
                # A document may legitimately link to a file outside the indexed
                # source tree. It is not a local relation.
                return None
            return resolved if resolved.is_file() else None
        except (OSError, ValueError):
            # Same rule for a link through an unreadable directory, an
            # over-long name or an embedded NUL: no relation, but no failure.
            return None

    @staticmethod
    def _is_text(media_type: str, path: Path) -> bool:
        """Return whether the file is scanned for links, by media type or suffix."""
        return media_type.startswith("text/") or path.suffix.casefold() in {
            ".html",
            ".htm",
            ".md",
            ".markdown",
            ".mdx",
        }
