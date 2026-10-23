"""Minimal Compound File Binary (OLE2) writer for parser test fixtures.

olefile can read but not create compound files, so the legacy Office parser
tests build them here. The writer emits a version-3 file (512-byte sectors)
with one FAT, a balanced directory tree and no mini stream: every stream is
zero-padded to the 4096-byte mini-stream cutoff so it lives in regular
sectors. Trailing zero padding is harmless for the Word, PowerPoint and BIFF
streams the tests build, because each locates its content by offset.
"""

from __future__ import annotations

import struct

SECTOR_SIZE = 512
MINI_STREAM_CUTOFF = 4096
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENTRIES_PER_FAT_SECTOR = SECTOR_SIZE // 4
_DIR_ENTRY = struct.Struct("<64sHBBIII16sIQQIQ")
_HEADER = struct.Struct("<8s16sHHHHH6sIIIIIIIII")


def _sector_count(size: int) -> int:
    return (size + SECTOR_SIZE - 1) // SECTOR_SIZE


def _sort_key(name: str) -> tuple[int, str]:
    # CFB orders siblings by UTF-16 length first, then case-insensitively.
    return (len(name.encode("utf-16-le")), name.upper())


def _dir_entry(
    name: str,
    object_type: int,
    links: tuple[int, int, int] = (NOSTREAM, NOSTREAM, NOSTREAM),
    extent: tuple[int, int] = (0, 0),
) -> bytes:
    """Pack one directory entry; ``links`` is (left, right, child), ``extent`` (start, size)."""

    encoded = name.encode("utf-16-le") + b"\x00\x00" if name else b""
    return _DIR_ENTRY.pack(
        encoded.ljust(64, b"\x00"),
        len(encoded),
        object_type,
        1,  # black
        *links,
        b"\x00" * 16,
        0,
        0,
        0,
        *extent,
    )


def _fat(chains: list[tuple[int, int]], fat_start: int, fat_sectors: int) -> list[int]:
    fat = [FREESECT] * (fat_sectors * _ENTRIES_PER_FAT_SECTOR)
    for start, count in chains:
        for offset in range(count):
            fat[start + offset] = start + offset + 1 if offset + 1 < count else ENDOFCHAIN
    for index in range(fat_sectors):
        fat[fat_start + index] = FATSECT
    return fat


def _directory(names: list[str], extents: list[tuple[int, int]], dir_sectors: int) -> bytes:
    """Entry 0 is the root; the sorted streams form a balanced search tree below it."""

    links: dict[int, tuple[int, int]] = {}

    def subtree(low: int, high: int) -> int:
        if low > high:
            return NOSTREAM
        middle = (low + high) // 2
        links[middle] = (subtree(low, middle - 1), subtree(middle + 1, high))
        return middle + 1

    root_child = subtree(0, len(names) - 1)
    entries = [_dir_entry("Root Entry", 5, (NOSTREAM, NOSTREAM, root_child), (ENDOFCHAIN, 0))]
    for index, name in enumerate(names):
        left, right = links[index]
        entries.append(_dir_entry(name, 2, (left, right, NOSTREAM), extents[index]))
    while len(entries) * _DIR_ENTRY.size < dir_sectors * SECTOR_SIZE:
        entries.append(_dir_entry("", 0))
    return b"".join(entries)


def _header(fat_sectors: int, dir_start: int, fat_start: int) -> bytes:
    difat = [fat_start + index for index in range(fat_sectors)]
    difat += [FREESECT] * (109 - len(difat))
    fields = (SIGNATURE, b"\x00" * 16, 0x003E, 0x0003, 0xFFFE, 9, 6, b"\x00" * 6)
    counts = (0, fat_sectors, dir_start, 0, MINI_STREAM_CUTOFF, ENDOFCHAIN, 0, ENDOFCHAIN, 0)
    return _HEADER.pack(*fields, *counts) + struct.pack("<109I", *difat)


def build_compound_file(streams: dict[str, bytes]) -> bytes:
    """Serialize root-level ``streams`` into a valid compound file."""

    names = sorted(streams, key=_sort_key)
    payloads = [streams[name].ljust(MINI_STREAM_CUTOFF, b"\x00") for name in names]
    extents: list[tuple[int, int]] = []
    next_sector = 0
    for payload in payloads:
        extents.append((next_sector, len(payload)))
        next_sector += _sector_count(len(payload))
    dir_start = next_sector
    dir_sectors = _sector_count((len(names) + 1) * _DIR_ENTRY.size)
    fat_sectors = 1
    while fat_sectors * _ENTRIES_PER_FAT_SECTOR < dir_start + dir_sectors + fat_sectors:
        fat_sectors += 1
    if fat_sectors > 109:
        raise ValueError("test compound files are limited to the header DIFAT")
    fat_start = dir_start + dir_sectors
    chains = [(start, _sector_count(size)) for start, size in extents]
    fat = _fat([*chains, (dir_start, dir_sectors)], fat_start, fat_sectors)

    body = b"".join(
        payload.ljust(_sector_count(len(payload)) * SECTOR_SIZE, b"\x00") for payload in payloads
    )
    body += _directory(names, extents, dir_sectors)
    body += struct.pack(f"<{len(fat)}I", *fat)
    return _header(fat_sectors, dir_start, fat_start) + body
