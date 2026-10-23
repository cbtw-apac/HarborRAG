"""Shared helpers for legacy Microsoft Office binary (OLE compound file) formats."""

from __future__ import annotations

import struct
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from io import BytesIO
from typing import Any

from harborrag_adapters.parsers.errors import (
    ParseError,
    PasswordProtectedError,
    UnsupportedFormatError,
)

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Library-level failures that a malformed binary stream can surface while it is
# being decoded by hand. They are converted to ParseError so a corrupt upload is
# quarantined instead of crashing the worker with an unexpected exception type.
BINARY_DECODE_ERRORS: tuple[type[BaseException], ...] = (
    struct.error,
    IndexError,
    UnicodeError,
    ValueError,
    KeyError,
    OverflowError,
    OSError,
    EOFError,
    RecursionError,
)


class ContainerKind(StrEnum):
    """Container shape detected from the leading bytes of an upload."""

    OLE = "ole"
    ZIP = "zip"
    RTF = "rtf"
    HTML = "html"
    XML = "xml"
    MHTML = "mhtml"
    UNKNOWN = "unknown"


def sniff_container(data: bytes) -> ContainerKind:
    """Classify an upload by signature, independent of its filename or MIME type.

    Legacy Office extensions are routinely reused for other payloads: Word can
    save RTF, HTML, MHTML or Word 2003 XML with a ``.doc`` name, and renamed
    OOXML packages are common in attachment stores.
    """

    if data.startswith(OLE_SIGNATURE):
        return ContainerKind.OLE
    if data.startswith(b"PK\x03\x04"):
        return ContainerKind.ZIP
    head = data[:512].lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n").lower()
    if head.startswith(b"{\\rtf"):
        return ContainerKind.RTF
    if head.startswith((b"<!doctype html", b"<html")):
        return ContainerKind.HTML
    if head.startswith(b"<?xml"):
        return ContainerKind.HTML if b"<html" in head else ContainerKind.XML
    if head.startswith(b"mime-version:"):
        return ContainerKind.MHTML
    return ContainerKind.UNKNOWN


def raise_unsupported_container(kind: ContainerKind, *, format_name: str) -> None:
    """Reject a payload whose real container is not the expected binary format."""

    labels = {
        ContainerKind.RTF: "Rich Text Format (RTF)",
        ContainerKind.HTML: "HTML",
        ContainerKind.XML: "XML",
        ContainerKind.MHTML: "MHTML web archive",
        ContainerKind.UNKNOWN: "an unrecognized format",
    }
    raise UnsupportedFormatError(
        f"Input labelled as {format_name} is actually {labels.get(kind, kind.value)}; "
        f"only binary {format_name} compound files are supported by this engine."
    )


def import_olefile(format_name: str) -> Any:
    """Import olefile lazily so the dependency stays optional."""

    try:
        import olefile
    except ImportError as exc:
        raise ParseError(
            f"{format_name} parsing requires `olefile`; install "
            "`harborrag-adapters[parsers]` or `pip install olefile`."
        ) from exc
    return olefile


@contextmanager
def open_ole(olefile: Any, data: bytes) -> Generator[Any, None, None]:
    """Open an in-memory compound file and always release it."""

    ole = olefile.OleFileIO(BytesIO(data))
    try:
        yield ole
    finally:
        ole.close()


def read_ole_stream(ole: Any, name: str, *, max_bytes: int) -> bytes:
    """Read one named stream, bounded by the size declared in the directory."""

    size = int(ole.get_size(name))
    if size < 0 or size > max_bytes:
        raise ParseError(f"Compound-file stream {name!r} declares an invalid size {size}")
    with ole.openstream(name) as stream:
        data = stream.read(size)
    if len(data) != size:
        raise ParseError(f"Compound-file stream {name!r} is truncated")
    return bytes(data)


def raise_if_ole_encrypted_package(ole: Any, *, format_name: str) -> None:
    """Report an encrypted OOXML package that was saved under a legacy name."""

    if ole.exists("EncryptedPackage") or ole.exists("EncryptionInfo"):
        raise PasswordProtectedError(f"{format_name} is password-protected")
