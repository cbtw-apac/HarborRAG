"""Builders for minimal legacy Office binary files (Word 97 `.doc`, PowerPoint `.ppt`)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from cfb_writer import build_compound_file

WORD97_NFIB = 0xC1
_FIB_TEXT_OFFSET = 1024
_CLX_PAIR_INDEX = 33
_FIB_RG_FC_LCB_PAIRS = 0x5D


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _pieces(text: str, compress: bool) -> list[tuple[str, bool]]:
    """Split text into runs; cp1252-encodable runs become compressed pieces."""

    if not compress:
        return [(text, False)]
    runs: list[tuple[str, bool]] = []
    for char in text:
        try:
            char.encode("cp1252")
            packed = True
        except UnicodeEncodeError:
            packed = False
        if runs and runs[-1][1] == packed:
            runs[-1] = (runs[-1][0] + char, packed)
        else:
            runs.append((char, packed))
    return runs


@dataclass(frozen=True)
class WordDoc:
    """Story text and FIB options for a synthetic Word 97 document."""

    main: str
    footnotes: str = ""
    headers: str = ""
    textboxes: str = ""
    header_textboxes: str = ""
    compress: bool = False
    encrypted: bool = False
    n_fib: int = WORD97_NFIB
    ident: int = 0xA5EC
    table_stream: str = "1Table"

    @property
    def stories(self) -> list[str]:
        # FibRgLw97 order: main, footnote, header, macro, comment, endnote, textbox, hdr textbox.
        return [
            self.main,
            self.footnotes,
            self.headers,
            "",
            "",
            "",
            self.textboxes,
            self.header_textboxes,
        ]

    def _text_and_clx(self) -> tuple[bytes, bytes]:
        stories = self.stories
        full = "".join(stories) + ("\r" if any(stories[1:]) else "")
        text_bytes = bytearray()
        cps = [0]
        fcs: list[int] = []
        for piece, packed in _pieces(full, self.compress):
            offset = _FIB_TEXT_OFFSET + len(text_bytes)
            fcs.append((offset * 2) | 0x40000000 if packed else offset)
            text_bytes += piece.encode("cp1252" if packed else "utf-16-le")
            cps.append(cps[-1] + _utf16_units(piece))
        pcds = b"".join(struct.pack("<HIH", 0, fc, 0) for fc in fcs)
        plc = struct.pack(f"<{len(cps)}I", *cps) + pcds
        prc = b"\x01" + struct.pack("<h", 2) + b"\x00\x00"
        return bytes(text_bytes), prc + b"\x02" + struct.pack("<I", len(plc)) + plc

    def _fib(self, clx_length: int) -> bytes:
        flags = 0x0004 | (0x0200 if self.table_stream == "1Table" else 0)
        flags |= 0x0100 if self.encrypted else 0
        base = struct.pack(
            "<7HI2B2H2I", self.ident, self.n_fib, 0, 0x0409, 0, flags, 0xBF, 0, 0, 0, 0, 0, 0, 0
        )
        lengths = [0] * 22
        for index, story in zip(range(3, 11), self.stories, strict=True):
            lengths[index] = _utf16_units(story)
        pairs = [0] * (_FIB_RG_FC_LCB_PAIRS * 2)
        pairs[_CLX_PAIR_INDEX * 2] = 16
        pairs[_CLX_PAIR_INDEX * 2 + 1] = clx_length
        return (
            base
            + struct.pack("<H", 14)
            + b"\x00" * 28
            + struct.pack("<H22i", 22, *lengths)
            + struct.pack(f"<H{len(pairs)}I", _FIB_RG_FC_LCB_PAIRS, *pairs)
            + struct.pack("<H", 0)
        )

    def to_bytes(self) -> bytes:
        text, clx = self._text_and_clx()
        word = self._fib(len(clx)).ljust(_FIB_TEXT_OFFSET, b"\x00") + text
        return build_compound_file({"WordDocument": word, self.table_stream: b"\x00" * 16 + clx})


def build_doc_bytes(main: str, **options: Any) -> bytes:
    """Build a Word 97 binary document; ``options`` are :class:`WordDoc` fields."""

    return WordDoc(main, **options).to_bytes()


def _record(record_type: int, payload: bytes, *, version: int = 0, instance: int = 0) -> bytes:
    return struct.pack("<HHI", version | (instance << 4), record_type, len(payload)) + payload


def _container(record_type: int, children: list[bytes], *, instance: int = 0) -> bytes:
    return _record(record_type, b"".join(children), version=0xF, instance=instance)


def _text_atoms(text: str, *, as_bytes: bool = False) -> bytes:
    header = _record(0x0F9F, struct.pack("<I", 1))
    if as_bytes:
        return header + _record(0x0FA8, text.encode("latin-1"))
    return header + _record(0x0FA0, text.encode("utf-16-le"))


def _drawing(text: str) -> bytes:
    textbox = _container(0xF00D, [_text_atoms(text)])
    shape = _container(0xF004, [textbox])
    return _container(0x040C, [_container(0xF002, [shape])])


def _slide_persist(persist_id: int, slide_id: int) -> bytes:
    return _record(0x03F3, struct.pack("<IIiII", persist_id, 0, 1, slide_id, 0))


@dataclass(frozen=True)
class PptSlide:
    """One slide: placeholder title (in the slide list), shape body, optional notes."""

    title: str
    body: str = ""
    notes: str = ""


def build_ppt_bytes(
    slides: list[PptSlide],
    *,
    master_text: str = "Click to edit Master title style",
    encryption: str = "",
    include_current_user: bool = True,
    stale_edit_pointer: bool = False,
) -> bytes:
    """Build a PowerPoint 97 binary file with a master, slides and notes.

    ``encryption``: ``"token"`` marks the Current User header as encrypted,
    ``"session"`` adds a CryptSession10Container referenced from the
    UserEditAtom, and ``"tail"`` adds that optional reference pointing at an
    ordinary record (not encrypted).
    """

    master_id, document_id = 1, 2
    slide_ids = [3 + index for index in range(len(slides))]
    notes_ids = [3 + len(slides) + index for index in range(len(slides))]

    master = _container(0x03F8, [_drawing(master_text)])
    slide_list = [
        part
        for index, slide in enumerate(slides)
        for part in (
            _slide_persist(slide_ids[index], 256 + index),
            _text_atoms(slide.title, as_bytes=True),
        )
    ]
    notes_list = [
        _slide_persist(notes_ids[index], 512 + index)
        for index, slide in enumerate(slides)
        if slide.notes
    ]
    document = _container(
        0x03E8,
        [
            _record(0x03E9, b"\x00" * 40, version=1),
            _container(
                0x0FF0,
                [_slide_persist(master_id, 0x80000000), _text_atoms(master_text)],
                instance=1,
            ),
            _container(0x0FF0, slide_list, instance=0),
            _container(0x0FF0, notes_list, instance=2),
        ],
    )
    containers: dict[int, bytes] = {master_id: master, document_id: document}
    for index, slide in enumerate(slides):
        notes_ref = 512 + index if slide.notes else 0
        atom = _record(
            0x03EF, struct.pack("<I8sIIHH", 0, b"\x00" * 8, 0x80000000, notes_ref, 0, 0), version=2
        )
        containers[slide_ids[index]] = _container(
            0x03EE, [atom, _drawing(slide.body)] if slide.body else [atom]
        )
        if slide.notes:
            containers[notes_ids[index]] = _container(
                0x03F0, [_record(0x03F1, b"\x00" * 8, version=1), _drawing(slide.notes)]
            )

    crypt_id = (notes_ids[-1] if notes_ids else 2) + 1
    if encryption == "session":
        containers[crypt_id] = _container(0x2F14, [_record(0x0FC8, b"\x00" * 8)])
    stream = bytearray()
    offsets: dict[int, int] = {}
    for persist_id in sorted(containers):
        offsets[persist_id] = len(stream)
        stream += containers[persist_id]
    ids = sorted(offsets)
    directory_offset = len(stream)
    # One PersistDirectoryEntry per id, since ids need not be contiguous.
    entries = b"".join(struct.pack("<II", i | (1 << 20), offsets[i]) for i in ids)
    stream += _record(0x1772, entries)
    edit_offset = len(stream)
    edit = struct.pack(
        "<IHBBIIIIHH", 0, 0, 0, 3, 0, directory_offset, document_id, ids[-1] + 1, 1, 0
    )
    if encryption in {"session", "tail"}:
        edit += struct.pack("<I", crypt_id if encryption == "session" else document_id)
    stream += _record(0x0FF5, edit)

    token = 0xF3D1C4DF if encryption == "token" else 0xE391C05F
    current_user = _record(
        0x0FF6,
        struct.pack(
            "<IIIHHBBH", 0x14, token, 0 if stale_edit_pointer else edit_offset, 0, 0x03F4, 3, 0, 0
        )
        + struct.pack("<I", 8),
    )
    streams = {"PowerPoint Document": bytes(stream)}
    if include_current_user:
        streams["Current User"] = current_user
    return build_compound_file(streams)
