"""Read the character stream of a Word 97-2003 binary document ([MS-DOC]).

Only the structures needed for text extraction are decoded: the File
Information Block (FIB) in the ``WordDocument`` stream, and the piece table
inside the CLX structure of the ``0Table``/``1Table`` stream. Every offset and
length read from the file is validated against the stream it points into, so a
hostile file cannot drive allocation beyond the size of its own streams.
"""

from __future__ import annotations

import struct
import sys
from array import array
from dataclasses import dataclass

from harborrag_adapters.parsers.errors import (
    ParseError,
    PasswordProtectedError,
    UnsupportedFormatError,
)

WORD_IDENT = 0xA5EC
MIN_WORD97_NFIB = 0xC1
_FLAG_ENCRYPTED = 0x0100
_FLAG_WHICH_TABLE = 0x0200
_FIB_BASE_SIZE = 32
_CLX_PAIR_INDEX = 33  # fcClx/lcbClx inside FibRgFcLcb97
_FC_COMPRESSED = 0x40000000
_FC_MASK = 0x3FFFFFFF
_PCD_SIZE = 8
_MAX_PRC_ENTRIES = 1 << 16

# Subdocument order in character-position space ([MS-DOC] 2.3.x); index 6 of
# FibRgLw97 is reserved (ccpMcr) and still occupies CP space when non-zero.
STORY_ORDER: tuple[tuple[str, int], ...] = (
    ("main", 3),
    ("footnote", 4),
    ("header", 5),
    ("macro", 6),
    ("comment", 7),
    ("endnote", 8),
    ("textbox", 9),
    ("header_textbox", 10),
)


@dataclass(frozen=True, slots=True)
class WordFib:
    """FIB fields needed to locate and bound the document text."""

    n_fib: int
    table_stream: str
    story_lengths: tuple[tuple[str, int], ...]
    fc_clx: int
    lcb_clx: int


@dataclass(frozen=True, slots=True)
class Piece:
    """One contiguous run of characters in the piece table."""

    cp_start: int
    cp_end: int
    byte_offset: int
    compressed: bool


def read_fib(word_stream: bytes) -> WordFib:
    """Decode the FIB and reject encrypted or pre-Word 97 documents."""

    if len(word_stream) < _FIB_BASE_SIZE + 2:
        raise ParseError("WordDocument stream is too short to contain a FIB")
    ident, n_fib = struct.unpack_from("<HH", word_stream, 0)
    # Word 2.0-95 files use other identifiers (0xA5DC, 0xA699, ...) in the same
    # 0xA5xx-0xA6xx range; report them as an unsupported version, not corruption.
    legacy_ident = 0xA500 <= ident <= 0xA6FF
    if ident != WORD_IDENT and not legacy_ident:
        raise ParseError(f"WordDocument stream has invalid FIB identifier 0x{ident:04X}")
    if n_fib < MIN_WORD97_NFIB or ident != WORD_IDENT:
        raise UnsupportedFormatError(
            f"Word document version nFib=0x{n_fib:04X} predates Word 97 "
            "(Word 6.0/95 binary files are not supported); re-save it as .docx."
        )
    (flags,) = struct.unpack_from("<H", word_stream, 0x0A)
    if flags & _FLAG_ENCRYPTED:
        raise PasswordProtectedError("DOC is password-protected or obfuscated")
    table_stream = "1Table" if flags & _FLAG_WHICH_TABLE else "0Table"

    offset = _FIB_BASE_SIZE
    (csw,) = struct.unpack_from("<H", word_stream, offset)
    offset += 2 + csw * 2
    (cslw,) = struct.unpack_from("<H", word_stream, offset)
    lw_offset = offset + 2
    if cslw < 11:
        raise ParseError(f"FibRgLw97 has {cslw} entries; at least 11 are required")
    lengths = struct.unpack_from(f"<{cslw}i", word_stream, lw_offset)
    offset = lw_offset + cslw * 4
    (cb_rg_fc_lcb,) = struct.unpack_from("<H", word_stream, offset)
    if cb_rg_fc_lcb <= _CLX_PAIR_INDEX:
        raise ParseError("FIB is too short to locate the piece table (CLX)")
    fc_clx, lcb_clx = struct.unpack_from("<II", word_stream, offset + 2 + _CLX_PAIR_INDEX * 8)

    story_lengths: list[tuple[str, int]] = []
    for name, index in STORY_ORDER:
        length = lengths[index]
        if length < 0:
            raise ParseError(f"FIB declares a negative {name} character count")
        story_lengths.append((name, length))
    return WordFib(
        n_fib=n_fib,
        table_stream=table_stream,
        story_lengths=tuple(story_lengths),
        fc_clx=fc_clx,
        lcb_clx=lcb_clx,
    )


def read_pieces(table_stream: bytes, fib: WordFib) -> list[Piece]:
    """Parse the CLX structure and return the piece table in CP order."""

    end = fib.fc_clx + fib.lcb_clx
    if fib.lcb_clx == 0 or end > len(table_stream):
        raise ParseError("Piece table (CLX) lies outside the table stream")
    offset = fib.fc_clx
    for _ in range(_MAX_PRC_ENTRIES):
        if offset >= end:
            raise ParseError("CLX does not contain a piece table (Pcdt)")
        clxt = table_stream[offset]
        if clxt == 0x01:  # Prc: property modifiers, skipped
            (cb_grpprl,) = struct.unpack_from("<h", table_stream, offset + 1)
            if cb_grpprl < 0:
                raise ParseError("CLX contains a negative Prc size")
            offset += 3 + cb_grpprl
            continue
        if clxt != 0x02:
            raise ParseError(f"CLX contains an unknown entry type 0x{clxt:02X}")
        (lcb,) = struct.unpack_from("<I", table_stream, offset + 1)
        start = offset + 5
        if lcb < 4 or start + lcb > end or (lcb - 4) % (4 + _PCD_SIZE):
            raise ParseError("Piece table (PlcPcd) has an invalid size")
        return _decode_plc_pcd(table_stream, start, (lcb - 4) // (4 + _PCD_SIZE))
    raise ParseError("CLX has too many property-modifier entries")


def _decode_plc_pcd(data: bytes, start: int, count: int) -> list[Piece]:
    cps = struct.unpack_from(f"<{count + 1}I", data, start)
    pcd_start = start + (count + 1) * 4
    pieces: list[Piece] = []
    for index in range(count):
        cp_start, cp_end = cps[index], cps[index + 1]
        if cp_end < cp_start:
            raise ParseError("Piece table character positions are not ascending")
        (fc_value,) = struct.unpack_from("<I", data, pcd_start + index * _PCD_SIZE + 2)
        compressed = bool(fc_value & _FC_COMPRESSED)
        fc = fc_value & _FC_MASK
        pieces.append(
            Piece(
                cp_start=cp_start,
                cp_end=cp_end,
                byte_offset=fc // 2 if compressed else fc,
                compressed=compressed,
            )
        )
    return pieces


def read_characters(word_stream: bytes, pieces: list[Piece], cp_limit: int) -> str:
    """Decode CPs ``[0, cp_limit)`` from the pieces that cover them."""

    chunks: list[str] = []
    for piece in pieces:
        if piece.cp_start >= cp_limit:
            break
        count = min(piece.cp_end, cp_limit) - piece.cp_start
        width = 1 if piece.compressed else 2
        begin = piece.byte_offset
        finish = begin + count * width
        if finish > len(word_stream):
            raise ParseError("Piece table points beyond the end of the WordDocument stream")
        raw = word_stream[begin:finish]
        if piece.compressed:
            chunks.append(raw.decode("cp1252", errors="replace"))
        else:
            # Keep one Python character per UTF-16 code unit so CP arithmetic
            # stays exact across surrogate pairs; `split_stories` re-joins them.
            units = array("H", raw)
            if sys.byteorder == "big":
                units.byteswap()
            chunks.append("".join(map(chr, units)))
    return "".join(chunks)


def split_stories(fib: WordFib, text: str) -> list[tuple[str, str]]:
    """Slice the decoded character stream into its named subdocuments."""

    stories: list[tuple[str, str]] = []
    position = 0
    for name, length in fib.story_lengths:
        units = text[position : position + length]
        stories.append(
            (name, units.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace"))
        )
        position += length
    return stories


def total_story_characters(fib: WordFib) -> int:
    """CP count spanned by every story, including the trailing paragraph mark."""

    lengths = [length for _, length in fib.story_lengths]
    total = sum(lengths)
    return total + 1 if any(lengths[1:]) else total
