from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
_HTML_LINK = re.compile(
    r"""(?:href|src)\s*=\s*["']([^"']+)["']""",
    flags=re.IGNORECASE,
)
_IGNORED_SCHEMES = frozenset({"data", "http", "https", "mailto", "tel", "javascript"})


class LocalDocumentRelationResolver:
    """Resolve document links beneath one configured filesystem trust root."""

    def __init__(self, root_path: Path) -> None:
        self._root_path = root_path.resolve()

    def relations(
        self,
        *,
        source_path: Path,
        content: bytes,
        media_type: str,
    ) -> list[dict[str, object]]:
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
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self._root_path):
            # A document may legitimately link to a file outside the indexed
            # source tree. It is not a local relation, and resolving it must
            # never turn ordinary document content into an ingestion failure.
            return None
        return resolved if resolved.is_file() else None

    @staticmethod
    def _is_text(media_type: str, path: Path) -> bool:
        return media_type.startswith("text/") or path.suffix.casefold() in {
            ".html",
            ".htm",
            ".md",
            ".markdown",
            ".mdx",
        }
