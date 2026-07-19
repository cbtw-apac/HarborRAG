from __future__ import annotations

import posixpath
import zipfile
from collections.abc import Callable
from typing import Any, ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .base import BaseParser
from .exceptions import ParseError
from .parser_logging import get_parser_logger, input_label, parser_log_extra
from .utils import guard_input_size, html_to_text, open_guarded_zip

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


class EpubParser(BaseParser[ParseInput, ParsedDocument]):
    """Extract reading-order text from EPUB archives."""

    parser_name: ClassVar[str] = "epub"
    parser_engine: ClassVar[str] = "python/zipfile+xml"
    suffixes: ClassVar[frozenset[str]] = frozenset({"epub"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/epub+zip"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Read EPUB HTML documents, convert them to text, and preserve section order."""

        parse_input = self.coerce_input(input)
        _ensure_defusedxml()
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
            with open_guarded_zip(guard_input_size(parse_input.read_bytes())) as archive:
                document_paths = self._document_paths(archive)
                sections: list[str] = []
                elements: list[DocumentElement] = []
                for index, path in enumerate(document_paths, start=1):
                    try:
                        html = archive.read(path)
                    except (KeyError, zipfile.BadZipFile, RuntimeError) as exc:
                        warnings.append(f"skipped section {path!r}: {exc}")
                        continue
                    text = html_to_text(html)
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
        except zipfile.BadZipFile as exc:
            parser_logger.warning(
                "Invalid EPUB archive: %s",
                input_label(parse_input),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=self.parser_name,
                    parser_engine=self.parser_engine,
                ),
            )
            raise ParseError("Invalid EPUB archive") from exc

        content = "\n\n".join(sections).strip()
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, sections=len(elements)),
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
                if (
                    item_id
                    and href
                    and media_type
                    in {
                        "application/xhtml+xml",
                        "text/html",
                    }
                ):
                    manifest[item_id] = posixpath.normpath(posixpath.join(opf_dir, href))
            elif tag == "itemref":
                idref = node.attrib.get("idref")
                if idref:
                    spine_ids.append(idref)

        ordered = [manifest[idref] for idref in spine_ids if idref in manifest]
        return ordered or cls._fallback_html_paths(names)

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
                    return path
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
