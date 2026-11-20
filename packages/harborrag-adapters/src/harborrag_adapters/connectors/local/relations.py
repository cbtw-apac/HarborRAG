"""Links between local documents, resolved to the source item IDs of their targets.

A link resolves only to an existing file beneath the configured root, so document
content can never point the connector at a file it does not index.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .filesystem_paths import file_extension

_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\((<[^<>\n]+>|[^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
_HTML_LINK = re.compile(
    r"""(?:href|src)\s*=\s*["']([^"']+)["']""",
    flags=re.IGNORECASE,
)
_IGNORED_SCHEMES = frozenset({"data", "http", "https", "mailto", "tel", "javascript"})


class LocalDocumentRelationResolver:
    """Resolve document links beneath one configured filesystem trust root.

    Args:
        root_path: Root of the local source. Its resolved form bounds every link.
    """

    def __init__(self, root_path: Path) -> None:
        self._root_path = root_path.resolve()

    def relations(
        self,
        *,
        source_path: Path,
        content: bytes,
        media_type: str,
    ) -> list[dict[str, object]]:
        """Return one ``links_to`` relation per local file a text document links to.

        Markdown links, angle-bracket destinations included, and HTML ``href`` and
        ``src`` values are read. Web, mail and data links, anchors, and links to
        missing files or files outside the root are left out.

        Args:
            source_path: Path of the document whose links are read.
            content: Document bytes, decoded as UTF-8.
            media_type: Media type of the document. Only text documents have links.

        Returns:
            Relations sorted by target ID, the target's path relative to the root,
            each keeping the link as written under ``source_link``.
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
        """Return the existing file a link points to beneath the root, or None.

        The link is split before it is percent-decoded, and its literal spaces are
        encoded first, so a target name may start with a space or hold ``#``.
        A document may link to a file outside the indexed tree. That link is not
        a local relation, and it never turns document content into an ingestion
        failure.
        """
        written = raw_target.strip().strip("<>")
        if not written or written.startswith("#"):
            return None
        parsed = urlsplit(written.replace(" ", "%20"))
        if parsed.scheme.casefold() in _IGNORED_SCHEMES or parsed.netloc:
            return None
        path_value = unquote(parsed.path)
        if not path_value:
            return None
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = source_path.parent / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self._root_path):
            return None
        return resolved if resolved.is_file() else None

    @staticmethod
    def _is_text(media_type: str, path: Path) -> bool:
        return media_type.startswith("text/") or file_extension(path) in {
            ".html",
            ".htm",
            ".md",
            ".markdown",
            ".mdx",
        }
