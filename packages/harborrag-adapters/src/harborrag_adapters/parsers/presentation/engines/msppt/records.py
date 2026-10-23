"""Record-level reader for the PowerPoint 97-2003 binary format ([MS-PPT]).

The ``PowerPoint Document`` stream is a sequence of records, each with an
8-byte header (version/instance, type, length). The live slides are found via
the ``Current User`` stream -> UserEditAtom chain -> persist directory, which
maps persist identifiers to stream offsets. This skips stale records left by
incremental ("fast") saves and never visits master slides, whose placeholder
prompts ("Click to edit Master title style") are not document content.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

from harborrag_adapters.parsers.errors import ParseError, PasswordProtectedError

RT_DOCUMENT = 0x03E8
RT_SLIDE = 0x03EE
RT_SLIDE_ATOM = 0x03EF
RT_NOTES = 0x03F0
RT_SLIDE_PERSIST_ATOM = 0x03F3
RT_MAIN_MASTER = 0x03F8
RT_SLIDE_LIST_WITH_TEXT = 0x0FF0
RT_TEXT_CHARS_ATOM = 0x0FA0
RT_TEXT_BYTES_ATOM = 0x0FA8
RT_USER_EDIT_ATOM = 0x0FF5
RT_CURRENT_USER_ATOM = 0x0FF6
RT_PERSIST_DIRECTORY_ATOM = 0x1772
RT_CRYPT_SESSION = 0x2F14
SLIDE_LIST_SLIDES = 0
SLIDE_LIST_NOTES = 2
HEADER_TOKEN_PLAIN = 0xE391C05F
HEADER_TOKEN_ENCRYPTED = 0xF3D1C4DF
_HEADER = struct.Struct("<HHI")
_MAX_DEPTH = 32
_MAX_EDITS = 4096
_SKIPPED_CONTAINERS = frozenset({RT_MAIN_MASTER})
_TEXT_TRANSLATION: dict[int, str | None] = {
    code: None for code in range(0x20) if chr(code) not in {"\t", "\n"}
}
_TEXT_TRANSLATION.update({0x0D: "\n", 0x0B: "\n", 0xFFFD: None})


@dataclass(frozen=True, slots=True)
class Record:
    """One record header plus the bounds of its payload."""

    version: int
    instance: int
    type: int
    start: int
    end: int

    @property
    def is_container(self) -> bool:
        return self.version == 0xF


@dataclass(slots=True)
class SlideEntry:
    """A SlidePersistAtom from a SlideListWithText and the text that follows it."""

    persist_id: int
    slide_id: int
    texts: list[str] = field(default_factory=list)


def read_record(data: bytes, offset: int, limit: int | None = None) -> Record:
    """Decode the record header at ``offset`` and bound its payload."""

    bound = len(data) if limit is None else limit
    if offset < 0 or offset + _HEADER.size > bound:
        raise ParseError(f"PowerPoint record header at offset {offset} is out of bounds")
    ver_inst, rec_type, length = _HEADER.unpack_from(data, offset)
    start = offset + _HEADER.size
    if start + length > bound:
        raise ParseError(f"PowerPoint record 0x{rec_type:04X} overruns its container")
    return Record(ver_inst & 0x000F, ver_inst >> 4, rec_type, start, start + length)


def iter_children(data: bytes, parent: Record) -> Iterator[Record]:
    """Yield the direct children of a container record."""

    offset = parent.start
    while offset + _HEADER.size <= parent.end:
        record = read_record(data, offset, parent.end)
        yield record
        offset = record.end


def decode_text_atom(data: bytes, record: Record) -> str | None:
    """Return the text of a TextCharsAtom/TextBytesAtom, or None for other records."""

    payload = data[record.start : record.end]
    if record.type == RT_TEXT_CHARS_ATOM:
        text = payload.decode("utf-16-le", errors="replace")
    elif record.type == RT_TEXT_BYTES_ATOM:
        # Each byte is the low byte of a UTF-16 code unit whose high byte is 0.
        text = payload.decode("latin-1")
    else:
        return None
    cleaned = text.translate(_TEXT_TRANSLATION).strip()
    # Slide-number/date placeholders store a lone "*" that PowerPoint expands.
    return cleaned if cleaned and cleaned != "*" else None


def collect_text(data: bytes, container: Record, depth: int = 0) -> list[str]:
    """Collect text atoms below a container, depth-first in stream order."""

    if depth > _MAX_DEPTH:
        raise ParseError("PowerPoint record nesting exceeds the supported depth")
    texts: list[str] = []
    for record in iter_children(data, container):
        if record.is_container:
            if record.type not in _SKIPPED_CONTAINERS:
                texts.extend(collect_text(data, record, depth + 1))
            continue
        text = decode_text_atom(data, record)
        if text:
            texts.append(text)
    return texts


def slide_list_entries(data: bytes, slide_list: Record) -> list[SlideEntry]:
    """Group a SlideListWithText's text atoms under their SlidePersistAtom."""

    entries: list[SlideEntry] = []
    for record in iter_children(data, slide_list):
        if record.type == RT_SLIDE_PERSIST_ATOM:
            persist_id, _flags, _texts, slide_id = struct.unpack_from("<IIiI", data, record.start)
            entries.append(SlideEntry(persist_id=persist_id, slide_id=slide_id))
            continue
        text = decode_text_atom(data, record)
        if text and entries:
            entries[-1].texts.append(text)
    return entries


def document_slide_lists(data: bytes, document: Record) -> dict[int, list[SlideEntry]]:
    """Return the slide and notes SlideListWithText entries of a Document container."""

    lists: dict[int, list[SlideEntry]] = {}
    for record in iter_children(data, document):
        if record.type == RT_SLIDE_LIST_WITH_TEXT and record.instance in {
            SLIDE_LIST_SLIDES,
            SLIDE_LIST_NOTES,
        }:
            lists.setdefault(record.instance, []).extend(slide_list_entries(data, record))
    return lists


def notes_id_of(data: bytes, slide: Record) -> int | None:
    """Return the notesIdRef recorded in a Slide container's SlideAtom."""

    for record in iter_children(data, slide):
        if record.type == RT_SLIDE_ATOM and record.end - record.start >= 20:
            (notes_id,) = struct.unpack_from("<I", data, record.start + 16)
            return notes_id or None
    return None


def current_edit_offset(current_user: bytes) -> int:
    """Read offsetToCurrentEdit from the ``Current User`` stream."""

    record = read_record(current_user, 0)
    if record.type != RT_CURRENT_USER_ATOM or record.end - record.start < 12:
        raise ParseError("PowerPoint Current User stream is malformed")
    _size, token, offset = struct.unpack_from("<III", current_user, record.start)
    if token == HEADER_TOKEN_ENCRYPTED:
        raise PasswordProtectedError("PPT is password-protected")
    if token != HEADER_TOKEN_PLAIN:
        raise ParseError(f"PowerPoint Current User header token 0x{token:08X} is invalid")
    return int(offset)


def persist_directory(data: bytes, edit_offset: int) -> tuple[dict[int, int], int]:
    """Follow the UserEditAtom chain and merge persist directories, newest first."""

    persist: dict[int, int] = {}
    document_persist_id: int | None = None
    encrypt_ref: int | None = None
    seen: set[int] = set()
    offset = edit_offset
    while offset not in seen:
        if len(seen) >= _MAX_EDITS:
            raise ParseError("PowerPoint edit history is too long")
        seen.add(offset)
        edit = read_record(data, offset)
        if edit.type != RT_USER_EDIT_ATOM or edit.end - edit.start < 0x1C:
            raise ParseError("PowerPoint UserEditAtom chain is malformed")
        last_edit, directory_offset, doc_id = struct.unpack_from("<III", data, edit.start + 8)
        if document_persist_id is None:
            document_persist_id = doc_id
            if edit.end - edit.start >= 0x20:
                # Optional encryptSessionPersistIdRef; confirmed against the persist map below.
                (encrypt_ref,) = struct.unpack_from("<I", data, edit.start + 0x1C)
        _merge_directory(data, read_record(data, directory_offset), persist)
        if last_edit == 0:
            break
        offset = last_edit
    if encrypt_ref is not None and encrypt_ref in persist:
        if read_record(data, persist[encrypt_ref]).type == RT_CRYPT_SESSION:
            raise PasswordProtectedError("PPT is password-protected")
    if document_persist_id is None or document_persist_id not in persist:
        raise ParseError("PowerPoint persist directory does not reference a document")
    return persist, document_persist_id


def _merge_directory(data: bytes, directory: Record, persist: dict[int, int]) -> None:
    if directory.type != RT_PERSIST_DIRECTORY_ATOM:
        raise ParseError("PowerPoint persist directory offset is invalid")
    offset = directory.start
    while offset + 4 <= directory.end:
        (entry,) = struct.unpack_from("<I", data, offset)
        first_id, count = entry & 0x000FFFFF, entry >> 20
        offset += 4
        if offset + count * 4 > directory.end:
            raise ParseError("PowerPoint persist directory entry overruns its record")
        for index, value in enumerate(struct.unpack_from(f"<{count}I", data, offset)):
            persist.setdefault(first_id + index, value)
        offset += count * 4


def top_level_records(data: bytes) -> Iterator[Record]:
    """Yield top-level records in stream order (used when no persist data exists)."""

    offset = 0
    while offset + _HEADER.size <= len(data):
        record = read_record(data, offset)
        if record.type == RT_CRYPT_SESSION:
            raise PasswordProtectedError("PPT is password-protected")
        yield record
        offset = record.end
