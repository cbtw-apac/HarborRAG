from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from harborrag_adapters.parsers.common.legacy_office import (
    BINARY_DECODE_ERRORS,
    ContainerKind,
    import_olefile,
    open_ole,
    raise_if_ole_encrypted_package,
    raise_unsupported_container,
    read_ole_stream,
    sniff_container,
)
from harborrag_adapters.parsers.common.normalization import compact_text
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import ParseResourceBudget, guard_input_size
from harborrag_adapters.parsers.errors import (
    ParseError,
    PasswordProtectedError,
    UnsupportedFormatError,
)
from harborrag_adapters.parsers.presentation.base import HarborPresentationEngine
from harborrag_adapters.parsers.presentation.engines.msppt import records as rec
from harborrag_adapters.parsers.presentation.engines.python_pptx.engine import (
    PythonPptxPresentationEngine,
)
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("ppt")
_DOCUMENT_STREAM = "PowerPoint Document"
_CURRENT_USER_STREAM = "Current User"


@dataclass(frozen=True, slots=True)
class _Slide:
    text: str
    notes: str


class MsPowerPointBinaryPresentationEngine(HarborPresentationEngine):
    """Extract slide text from legacy PowerPoint 97-2003 binary `.ppt` files."""

    supports_speaker_notes: ClassVar[bool] = True

    parser_name: ClassVar[str] = "ppt"
    parser_engine: ClassVar[str] = "olefile"
    # Not ".pot": that suffix is also a gettext template, which must keep reaching the
    # text family. A PowerPoint template still routes here by its MIME type.
    suffixes: ClassVar[frozenset[str]] = frozenset({"ppt", "pps"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/vnd.ms-powerpoint"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Return one element per slide with text, like the PPTX engine."""

        parse_input = self.coerce_input(input)
        source_bytes = guard_input_size(read_parse_input_bytes(parse_input))
        if not source_bytes:
            return self.empty_result(parse_input, slide_count=0)
        kind = sniff_container(source_bytes)
        if kind is ContainerKind.ZIP:
            # A renamed OOXML presentation: the python-pptx engine owns it.
            return PythonPptxPresentationEngine().parse(parse_input)
        if kind is not ContainerKind.OLE:
            raise_unsupported_container(kind, format_name="PowerPoint .ppt")
        olefile = import_olefile("Legacy PowerPoint .ppt")
        try:
            slides = self._read_slides(olefile, source_bytes)
        except ParseError:
            raise
        except BINARY_DECODE_ERRORS as exc:
            raise ParseError(f"{self.parser_engine} failed to parse PowerPoint: {exc}") from exc

        sections: list[str] = []
        elements: list[DocumentElement] = []
        for index, slide in enumerate(slides, start=1):
            if slide.text:
                elements.append(
                    DocumentElement(
                        id=f"ppt:slide:{index}",
                        type="paragraph",
                        content=slide.text,
                        metadata={"slide": index},
                    )
                )
            if slide.notes:
                elements.append(
                    DocumentElement(
                        id=f"ppt:slide:{index}:notes",
                        type="paragraph",
                        content=slide.notes,
                        metadata={"slide": index, "part": "notes"},
                    )
                )
            body = "\n".join(
                part
                for part in (slide.text, f"Notes:\n{slide.notes}" if slide.notes else "")
                if part
            )
            if body:
                sections.append(f"Slide {index}\n{body}")
        content = "\n\n".join(sections).strip()
        ParseResourceBudget().consume_output(len(content))
        parser_logger.info(
            "Parsed PPT %s slides=%d content_chars=%d elements=%d",
            input_label(parse_input),
            len(slides),
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                input_bytes=len(source_bytes),
                slides=len(slides),
                content_chars=len(content),
                elements=len(elements),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, slide_count=len(slides)),
        )

    @classmethod
    def _read_slides(cls, olefile: Any, source_bytes: bytes) -> list[_Slide]:
        limit = len(source_bytes)
        with open_ole(olefile, source_bytes) as ole:
            if not ole.exists(_DOCUMENT_STREAM):
                raise_if_ole_encrypted_package(ole, format_name="PPT")
                raise UnsupportedFormatError(
                    "Compound file has no PowerPoint Document stream; "
                    "it is not a PowerPoint presentation."
                )
            if ole.exists("EncryptedSummary"):
                raise PasswordProtectedError("PPT is password-protected")
            data = read_ole_stream(ole, _DOCUMENT_STREAM, max_bytes=limit)
            current_user = (
                read_ole_stream(ole, _CURRENT_USER_STREAM, max_bytes=limit)
                if ole.exists(_CURRENT_USER_STREAM)
                else None
            )
        if current_user is None:
            return cls._slides_from_stream_order(data)
        try:
            return cls._slides_from_persist_directory(data, current_user)
        except PasswordProtectedError:
            raise
        except ParseError as persist_error:
            # Third-party writers sometimes leave a stale Current User pointer;
            # the records themselves are often intact, so fall back to them.
            try:
                return cls._slides_from_stream_order(data)
            except ParseError:
                raise persist_error from None

    @classmethod
    def _slides_from_persist_directory(cls, data: bytes, current_user: bytes) -> list[_Slide]:
        persist, document_id = rec.persist_directory(data, rec.current_edit_offset(current_user))
        document = rec.read_record(data, persist[document_id])
        if document.type != rec.RT_DOCUMENT:
            raise ParseError("PowerPoint persist directory does not point at a Document")
        lists = rec.document_slide_lists(data, document)
        notes_by_id = {entry.slide_id: entry for entry in lists.get(rec.SLIDE_LIST_NOTES, [])}
        slides: list[_Slide] = []
        for entry in lists.get(rec.SLIDE_LIST_SLIDES, []):
            texts = list(entry.texts)
            notes: list[str] = []
            slide = cls._persisted(data, persist, entry.persist_id, rec.RT_SLIDE)
            if slide is not None:
                texts.extend(rec.collect_text(data, slide))
                notes_entry = notes_by_id.get(rec.notes_id_of(data, slide) or -1)
                if notes_entry is not None:
                    notes.extend(notes_entry.texts)
                    container = cls._persisted(data, persist, notes_entry.persist_id, rec.RT_NOTES)
                    if container is not None:
                        notes.extend(rec.collect_text(data, container))
            slides.append(_Slide(_join(texts), _join(notes)))
        return slides

    @staticmethod
    def _persisted(
        data: bytes, persist: dict[int, int], persist_id: int, record_type: int
    ) -> rec.Record | None:
        offset = persist.get(persist_id)
        if offset is None:
            return None
        record = rec.read_record(data, offset)
        return record if record.type == record_type else None

    @staticmethod
    def _slides_from_stream_order(data: bytes) -> list[_Slide]:
        """Fallback without a Current User stream: pair slide lists and containers by order."""

        entries: list[rec.SlideEntry] = []
        containers: list[rec.Record] = []
        for record in rec.top_level_records(data):
            if record.type == rec.RT_DOCUMENT:
                entries.extend(
                    rec.document_slide_lists(data, record).get(rec.SLIDE_LIST_SLIDES, [])
                )
            elif record.type == rec.RT_SLIDE:
                containers.append(record)
        slides: list[_Slide] = []
        for index in range(max(len(entries), len(containers))):
            texts = list(entries[index].texts) if index < len(entries) else []
            if index < len(containers):
                texts.extend(rec.collect_text(data, containers[index]))
            slides.append(_Slide(_join(texts), ""))
        return slides


def _join(texts: list[str]) -> str:
    # A placeholder's text normally lives either in the slide list or in the
    # shape, but some writers store it in both; drop exact repeats.
    return compact_text("\n".join(dict.fromkeys(texts)))


PptParser = MsPowerPointBinaryPresentationEngine
