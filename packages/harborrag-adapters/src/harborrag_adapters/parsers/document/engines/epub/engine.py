from __future__ import annotations

import posixpath
import zipfile
from collections.abc import Callable
from typing import Any, ClassVar

from harborrag_adapters.parsers.common.normalization import html_to_text
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import guard_input_size, open_guarded_zip
from harborrag_adapters.parsers.document.base import HarborDocumentEngine
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("epub")

_xml_fromstring: Callable[[bytes], Any] | None = None
_XmlParseError: type[BaseException] | None = None


def _ensure_defusedxml() -> tuple[Callable[[bytes], Any], type[BaseException]]:
    """Import defusedxml lazily, failing closed if it is not installed.

    Falling back to ``xml.etree.ElementTree`` would parse untrusted EPUB
    content with a parser known to be vulnerable to XML attacks (billion
    laughs, external entity expansion). Requiring defusedxml keeps that
    boundary closed instead of silently degrading.
    """
    global _xml_fromstring, _XmlParseError
    if _xml_fromstring is None or _XmlParseError is None:
        try:
            from defusedxml.ElementTree import ParseError as xml_parse_error
            from defusedxml.ElementTree import fromstring as xml_fromstring
        except ImportError as exc:
            raise ParseError(
                "EPUB parsing requires `defusedxml`; install "
                "`harborrag-adapters[parsers]` or `pip install defusedxml`."
            ) from exc
        _xml_fromstring = xml_fromstring
        _XmlParseError = xml_parse_error
    return _xml_fromstring, _XmlParseError


class EpubDocumentEngine(HarborDocumentEngine):
    """Extract reading-order text from EPUB archives."""

    parser_name: ClassVar[str] = "epub"
    parser_engine: ClassVar[str] = "python/zipfile+xml"
    suffixes: ClassVar[frozenset[str]] = frozenset({"epub"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/epub+zip"})

    def parse(self, input: ParseInput) -> ParsedDocument:  # noqa: C901
        """Read EPUB HTML documents, convert them to text, and preserve section order."""

        parse_input = self.coerce_input(input)
        _ensure_defusedxml()
        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        if not source_bytes:
            # 0 bytes is never a valid zip archive, so zipfile would
            # otherwise reject it as corrupt. There is nothing to parse, so
            # succeed with empty output like the other engines.
            return self.empty_result(parse_input, sections=0, title=None)
        try:
            parser_logger.debug(
                "Extracting EPUB text from %s",
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            warnings: list[str] = []
            with open_guarded_zip(source_bytes) as archive:
                document_paths = self._document_paths(archive)
                publication_title = self._publication_title(archive)
                discovered_title: str | None = None
                sections: list[str] = []
                elements: list[DocumentElement] = []
                for index, path in enumerate(document_paths, start=1):
                    try:
                        html = archive.read(path)
                    except (KeyError, zipfile.BadZipFile, RuntimeError) as exc:
                        warnings.append(f"skipped section {path!r}: {exc}")
                        continue
                    section_title = self._html_title(html)
                    discovered_title = discovered_title or section_title
                    text = html_to_text(html)
                    if section_title:
                        text = self._deduplicate_leading_title(text, section_title)
                    if publication_title:
                        text = self._remove_leading_title(text, publication_title)
                    if not text:
                        continue
                    sections.append(text)
                    elements.append(
                        DocumentElement(
                            id=f"epub:section:{index}",
                            type="paragraph",
                            content=text,
                            metadata={"path": path, "order": index},
                        )
                    )
        except ParseError:
            # `ParseError` is itself a `RuntimeError` subclass (see
            # harborrag_adapters.parsers.errors), so it would otherwise be
            # caught and flattened by the broader `RuntimeError` clause below
            # -- re-raise typed errors already raised deeper in this method
            # (e.g. a missing OPF member) unchanged.
            raise
        except (zipfile.BadZipFile, RuntimeError) as exc:
            # `zipfile` raises a bare `RuntimeError` (not `BadZipFile`) when a
            # member is password-protected/encrypted. Surface that as the same
            # typed, recoverable `ParseError` instead of letting it crash the
            # caller -- container.xml/the OPF are read before any per-section
            # try/except gets a chance to catch it.
            is_encrypted = isinstance(exc, RuntimeError) and "encrypted" in str(exc).lower()
            parser_logger.warning(
                "%s: %s",
                "Encrypted EPUB archive" if is_encrypted else "Invalid EPUB archive",
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            if is_encrypted:
                raise ParseError("EPUB archive is password-protected/encrypted") from exc
            raise ParseError("Invalid EPUB archive") from exc

        title = publication_title or discovered_title
        if publication_title:
            sections.insert(0, publication_title)
            elements.insert(
                0,
                DocumentElement(
                    id="epub:title",
                    type="heading",
                    content=publication_title,
                    metadata={"level": 1},
                ),
            )
        content = "\n\n".join(sections).strip()
        section_count = sum(element.id.startswith("epub:section:") for element in elements)
        parser_logger.info(
            "Parsed EPUB %s sections=%d content_chars=%d elements=%d warnings=%d",
            input_label(parse_input),
            section_count,
            len(content),
            len(elements),
            len(warnings),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                sections=section_count,
                content_chars=len(content),
                elements=len(elements),
                warnings=len(warnings),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(
                parse_input,
                sections=section_count,
                title=title,
            ),
            warnings=warnings or None,
        )

    @classmethod
    def _document_paths(cls, archive: zipfile.ZipFile) -> list[str]:
        """Resolve content documents from the OPF spine with an HTML fallback."""

        xml_fromstring, xml_parse_error = _ensure_defusedxml()
        names = set(archive.namelist())
        opf_path = cls._opf_path(archive, xml_fromstring, xml_parse_error)
        if opf_path is None:
            return cls._fallback_html_paths(names)

        try:
            root = xml_fromstring(archive.read(opf_path))
        except KeyError as exc:
            parser_logger.warning(
                "EPUB package document %s referenced by container.xml is missing",
                opf_path,
                extra=parser_log_extra(parser_name=cls.parser_name, opf_path=opf_path),
            )
            raise ParseError(f"EPUB package document {opf_path!r} is missing") from exc
        except xml_parse_error as exc:
            parser_logger.warning(
                "Invalid EPUB package document in %s",
                opf_path,
                extra=parser_log_extra(parser_name=cls.parser_name, opf_path=opf_path),
            )
            raise ParseError("Invalid EPUB package document") from exc

        opf_dir = posixpath.dirname(opf_path)
        manifest: dict[str, str] = {}
        spine_ids: list[str] = []

        for node in root.iter():
            tag = cls._local_name(node.tag)
            if tag == "item":
                item_id = node.attrib.get("id")
                href = node.attrib.get("href")
                media_type = node.attrib.get("media-type", "")
                properties = node.attrib.get("properties", "").split()
                if (
                    item_id
                    and href
                    and media_type
                    in {
                        "application/xhtml+xml",
                        "text/html",
                    }
                    and "nav" not in properties
                ):
                    # The EPUB3 navigation document (``properties="nav"``) is a
                    # generated table of contents, not reading content. Some
                    # spines still list it (so readers can open it as a page),
                    # but extracting it as a section duplicates the chapter
                    # titles it links to. The fallback path already excludes
                    # `nav.xhtml`/`toc.xhtml` by name for the same reason.
                    manifest[item_id] = posixpath.normpath(posixpath.join(opf_dir, href))
            elif tag == "itemref":
                idref = node.attrib.get("idref")
                if idref:
                    spine_ids.append(idref)

        ordered = [manifest[idref] for idref in spine_ids if idref in manifest]
        return ordered or cls._fallback_html_paths(names)

    @classmethod
    def _publication_title(cls, archive: zipfile.ZipFile) -> str | None:
        """Read the package title used when section markup omits it."""

        xml_fromstring, xml_parse_error = _ensure_defusedxml()
        opf_path = cls._opf_path(archive, xml_fromstring, xml_parse_error)
        if opf_path is None:
            return None
        try:
            root = xml_fromstring(archive.read(opf_path))
        except (KeyError, xml_parse_error):
            return None
        for node in root.iter():
            if cls._local_name(node.tag) != "title":
                continue
            value = " ".join("".join(node.itertext()).split())
            if value:
                return value
        return None

    @classmethod
    def _html_title(cls, html: bytes) -> str | None:
        """Extract a valid XHTML title for de-duplication and metadata."""

        xml_fromstring, xml_parse_error = _ensure_defusedxml()
        try:
            root = xml_fromstring(html)
        except xml_parse_error:
            return None
        for node in root.iter():
            if cls._local_name(node.tag) != "title":
                continue
            value = " ".join("".join(node.itertext()).split())
            if value:
                return value
        return None

    @staticmethod
    def _deduplicate_leading_title(text: str, title: str) -> str:
        """Collapse repeated HTML ``title``/body-heading text to one line."""

        lines = text.splitlines()
        normalized_title = " ".join(title.split()).casefold()
        repeated = 0
        for line in lines:
            if " ".join(line.split()).casefold() != normalized_title:
                break
            repeated += 1
        if repeated > 1:
            del lines[1:repeated]
        return "\n".join(lines).strip()

    @staticmethod
    def _remove_leading_title(text: str, title: str) -> str:
        """Remove a package title from section text before adding it once."""

        lines = text.splitlines()
        normalized_title = " ".join(title.split()).casefold()
        while lines and " ".join(lines[0].split()).casefold() == normalized_title:
            lines.pop(0)
        return "\n".join(lines).strip()

    @classmethod
    def _opf_path(
        cls,
        archive: zipfile.ZipFile,
        xml_fromstring: Callable[[bytes], Any],
        xml_parse_error: type[BaseException],
    ) -> str | None:
        """Locate the package document declared by META-INF/container.xml."""

        try:
            container = xml_fromstring(archive.read("META-INF/container.xml"))
        except (KeyError, xml_parse_error):
            return None

        for node in container.iter():
            if cls._local_name(node.tag) == "rootfile":
                path = node.attrib.get("full-path")
                if path:
                    return str(path)
        return None

    @staticmethod
    def _fallback_html_paths(names: set[str]) -> list[str]:
        """Return a stable list of likely content files when the spine is unavailable."""

        return sorted(
            name
            for name in names
            if name.lower().endswith((".html", ".htm", ".xhtml"))
            and not name.lower().endswith(("nav.xhtml", "toc.xhtml"))
        )

    @staticmethod
    def _local_name(tag: str) -> str:
        """Remove an XML namespace prefix from an ElementTree tag."""

        return tag.rsplit("}", 1)[-1]


EpubParser = EpubDocumentEngine
