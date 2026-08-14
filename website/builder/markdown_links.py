"""MarkdownLinksMixin implementation."""

import re
from pathlib import Path
from urllib.parse import quote


class MarkdownLinksMixin:
    """Focused Markdown operations composed by ``MarkdownProcessor``."""

    _ROOT_DOCUMENTS = frozenset(
        {
            "LICENSE",
            "LICENSE.md",
            "README.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
        }
    )
    _WELL_KNOWN_DOCUMENTS = frozenset(
        {"LICENSE", "README", "CHANGELOG", "CONTRIBUTING", "SECURITY"}
    )

    def extract_title_from_markdown(self, markdown_content: str) -> str:
        """Extract title from markdown content."""
        lines = markdown_content.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
        return "Documentation"  # Default fallback title

    def basic_markdown_to_html(self, markdown_content: str) -> str:
        """Basic markdown to HTML conversion - alias for compatibility."""
        return self.markdown_to_html(markdown_content)

    def convert_markdown_links_to_html(
        self, content: str, source_file: str = "", target_dir: str = ""
    ) -> str:
        """Convert markdown links to HTML format."""

        # Convert [text](link.md) to [text](link.html) - markdown style
        def replace_md_links(match):
            text = match.group(1)
            link = match.group(2)
            link = self._process_link_path(link, source_file)
            return f"[{text}]({link})"

        # Convert href="link.md" to href="link.html" - HTML style
        def replace_href_links(match):
            prefix = match.group(1)
            link = match.group(2)
            suffix = match.group(3)
            link = self._process_link_path(link, source_file)
            return f"{prefix}{link}{suffix}"

        # Process every link once so repository-owned files outside docs can
        # become source links instead of broken generated-site paths.
        content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_md_links, content)
        content = re.sub(r'(href=")([^\"]+)(")', replace_href_links, content)

        # Compatibility passes for callers that provide partially converted content.
        # Catch .md files and well-known files without extensions
        well_known_link_pattern_md = (
            r"\[([^\]]+)\]\(((?:(?:\.\./)+|\./|/)?"
            r"(?:LICENSE|README|CHANGELOG|CONTRIBUTING|SECURITY)(?:/[^)]*)?(?:#[^)]*)?)\)"
        )
        well_known_link_pattern_href = (
            r'(href=")((?:(?:\.\./)+|\./|/)?'
            r'(?:LICENSE|README|CHANGELOG|CONTRIBUTING|SECURITY)(?:/[^"]*)?(?:#[^"]*)?)(")'
        )

        content = re.sub(r"\[([^\]]+)\]\(([^)]+\.md(?:#[^)]*)?)\)", replace_md_links, content)
        content = re.sub(
            well_known_link_pattern_md,
            replace_md_links,
            content,
        )
        content = re.sub(r'(href=")([^"]+\.md(?:#[^"]*)?)(")', replace_href_links, content)
        content = re.sub(
            well_known_link_pattern_href,
            replace_href_links,
            content,
        )

        # The following normalizations are only applied during site builds (when source_file is provided).
        # Unit tests expect relative paths to be preserved.
        if source_file:
            # Normalize links that incorrectly include an extra "/docs/" prefix inside /docs pages
            # e.g., href="docs/users/..." when already under /docs/ -> make it absolute "/docs/users/..."
            content = re.sub(r'(href=")(docs/[^"]+)(")', r"\1/\2\3", content)
            content = re.sub(r"\]\((docs/[^)]+)\)", r"](/\1)", content)

            # Collapse accidental duplicate docs/docs prefixes
            content = re.sub(r'(href=")/?docs/docs/([^"]+)(")', r"\1/docs/\2\3", content)
            content = re.sub(r"\]\(/?docs/docs/([^\)]+)\)", r"](/docs/\1)", content)

            # Rewrite relative ./docs/... links to absolute /docs/ (HTML and Markdown)
            content = re.sub(r'(href=")\./docs/([^"#]*)(#[^"]*)?(")', r"\1/docs/\2\3\4", content)
            content = re.sub(r"\]\(\./docs/([^\)#]*)(#[^\)]*)?\)", r"](/docs/\1\2)", content)

            # Rewrite relative ../../docs/... links to absolute /docs/ (HTML and Markdown)
            content = re.sub(
                r'(href=")(?:\.{2}/)+docs/([^"#]*)(#[^"]*)?(")',
                r"\1/docs/\2\3\4",
                content,
            )
            content = re.sub(
                r"\]\((?:\.{2}/)+docs/([^\)#]*)(#[^\)]*)?\)", r"](/docs/\1\2)", content
            )

            # Convert .md (with optional anchors) to .html in both HTML and Markdown links
            content = re.sub(
                r'(href=")([^"\s]+)\.md(#[^"]*)?(")',
                lambda m: f"{m.group(1)}{m.group(2)}.html{m.group(3) or ''}{m.group(4)}",
                content,
            )
            content = re.sub(
                r"\]\(([^\)\s]+)\.md(#[^\)]*)?\)",
                lambda m: f"]({m.group(1)}.html{m.group(2) or ''})",
                content,
            )

            # Normalize developers relative links to directory indexes
            content = re.sub(
                r'(href=")\./(architecture|testing|deployment|extending)\.html(")',
                r"\1./\2/\3",
                content,
            )
            # Normalize absolute developers/*.html to directory indexes
            content = re.sub(
                r'(href=")([^"\s]*/developers/)(architecture|testing|deployment|extending)\.html(")',
                r"\1\2\3/\4",
                content,
            )
            content = re.sub(
                r"\]\(([^\)\s]*/developers/)(architecture|testing|deployment|extending)\.html\)",
                r"](\1\2/)",
                content,
            )
            # Normalize parent-relative developers links like ../extending.html to ../extending/
            content = re.sub(
                r'(href=")([^"#]*/developers/)(architecture|testing|deployment|extending)\.html(#[^"]*)?(")',
                r"\1\2\3/\4\5",
                content,
            )
            # Normalize sibling links such as ../extending.html -> ../extending/
            content = re.sub(
                r'(href=")\.\./(architecture|testing|deployment|extending)\.html(#[^"]*)?(")',
                r"\1../\2/\3\4",
                content,
            )
            content = re.sub(
                r"\]\(\.\./(architecture|testing|deployment|extending)\.html(#[^\)]*)?\)",
                r"](../\1/\2)",
                content,
            )

            # Ensure well-known repo root files under /docs have .html extension
            content = re.sub(
                r'(href=")(/docs/(?:LICENSE|README|CHANGELOG|CONTRIBUTING|SECURITY))(#[^"]*)?(")',
                r"\1\2.html\3\4",
                content,
            )

            # If a target output path is provided, convert absolute /docs/... links to relative ones
            if target_dir:
                try:
                    import posixpath

                    base_dir = target_dir
                    if not base_dir.endswith("/"):
                        base_dir = posixpath.dirname(base_dir) + "/"

                    def _to_relative_html(match: re.Match) -> str:
                        prefix, path_part, anchor, suffix = (
                            match.group(1),
                            match.group(2),
                            match.group(3) or "",
                            match.group(4),
                        )
                        abs_path = "docs/" + path_part
                        rel = posixpath.relpath(abs_path, base_dir.rstrip("/"))
                        return f"{prefix}{rel}{anchor or ''}{suffix}"

                    def _to_relative_md(match: re.Match) -> str:
                        path_part, anchor = match.group(1), match.group(2) or ""
                        abs_path = "docs/" + path_part
                        rel = posixpath.relpath(abs_path, base_dir.rstrip("/"))
                        return f"]({rel}{anchor})"

                    content = re.sub(
                        r'(href=")/docs/([^"#]+)(#[^"]*)?(")',
                        _to_relative_html,
                        content,
                    )
                    content = re.sub(r"\]\(/docs/([^\)#]+)(#[^\)]*)?\)", _to_relative_md, content)
                except Exception:
                    # Fallback silently if relative conversion fails
                    pass

        return content

    def _process_link_path(self, link: str, source_file: str = "") -> str:
        """Process a link path for conversion."""
        if re.match(r"^[a-z][a-z0-9+.-]*:", link, re.IGNORECASE) or link.startswith("//"):
            return link

        link, anchor = self._split_link_anchor(link)
        root_document_url = self._root_document_url(link)
        if root_document_url:
            return root_document_url + anchor

        if source_file:
            repository_link = self._repository_source_link(link, source_file)
            if repository_link:
                return repository_link + anchor

        link = self._normalize_docs_link(link, source_file)
        link = self._convert_document_suffix(link, anchor, source_file)
        link = self._finalize_docs_link(link, source_file)
        return link + anchor

    @staticmethod
    def _split_link_anchor(link: str) -> tuple[str, str]:
        """Split a link into its path and optional fragment."""
        path, separator, fragment = link.partition("#")
        anchor = f"#{fragment}" if separator else ""
        return path, anchor

    def _root_document_url(self, link: str) -> str:
        """Return the published URL for a repository-level document."""
        root_file = re.sub(r"^(?:(?:\.\./)+|\./|/+)", "", link)
        if root_file not in self._ROOT_DOCUMENTS:
            return ""

        output_name = "LICENSE" if root_file.startswith("LICENSE") else Path(root_file).stem
        return f"/docs/{output_name}.html"

    @staticmethod
    def _normalize_docs_link(link: str, source_file: str) -> str:
        """Normalize relative docs paths when a source-file context is available."""
        if not source_file:
            return link

        link = re.sub(r"^(?:\.{2}/)+docs/", "/docs/", link)
        link = re.sub(r"^\./docs/", "/docs/", link)
        return "/" + link if link.startswith("docs/") else link

    def _convert_document_suffix(self, link: str, anchor: str, source_file: str) -> str:
        """Convert Markdown and well-known document links to published HTML paths."""
        if link.endswith(".md"):
            preserve_bare_anchor = bool(anchor and "/" not in link and not source_file)
            return link if preserve_bare_anchor else link[:-3] + ".html"

        filename = link.rsplit("/", 1)[-1]
        if filename.upper() not in self._WELL_KNOWN_DOCUMENTS or "." in filename:
            return link

        if source_file and not link.startswith("/docs/"):
            link = "/docs/" + filename
        return link + ".html"

    @staticmethod
    def _finalize_docs_link(link: str, source_file: str) -> str:
        """Collapse duplicate docs prefixes and make build-time docs paths absolute."""
        link = re.sub(r"^/docs/docs/", "/docs/", link)
        link = link.replace("docs/docs/", "docs/")
        if source_file and link.startswith("docs/"):
            return "/" + link
        return link

    def _repository_source_link(self, link: str, source_file: str) -> str:
        """Link existing repository files outside ``docs`` to their GitHub source."""
        if not link or link.startswith(("/", "#")):
            return ""

        try:
            repository_root = Path.cwd().resolve()
            source_path = Path(source_file)
            if not source_path.is_absolute():
                source_path = repository_root / source_path
            target_path = (source_path.parent / link).resolve()
            relative_target = target_path.relative_to(repository_root)
        except (OSError, ValueError):
            return ""

        if relative_target.parts and relative_target.parts[0] == "docs":
            return ""
        if not target_path.exists():
            return ""

        repository_url = getattr(
            self, "repository_url", "https://github.com/cbtw-apac/HarborRAG"
        ).rstrip("/")
        view = "tree" if target_path.is_dir() else "blob"
        encoded_path = quote(relative_target.as_posix(), safe="/")
        return f"{repository_url}/{view}/main/{encoded_path}"
