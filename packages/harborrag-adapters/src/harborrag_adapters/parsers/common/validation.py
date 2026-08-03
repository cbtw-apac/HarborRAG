from __future__ import annotations

import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from io import BytesIO

from harborrag_adapters.parsers.errors import ParseError, PasswordProtectedError

DEFAULT_MAX_INPUT_BYTES = 512 * 1024 * 1024  # 512 MiB raw input
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB total
MAX_ARCHIVE_COMPRESSION_RATIO = 200  # per-member uncompressed / compressed
_OLE_COMPOUND_FILE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OOXML_ENCRYPTION_STREAMS = (
    "EncryptedPackage".encode("utf-16-le"),
    "EncryptionInfo".encode("utf-16-le"),
)


@contextmanager
def wrap_parse_errors(engine: str) -> Generator[None, None, None]:
    """Normalize third-party parsing failures into :class:`ParseError`.

    Concrete parsers wrap the library call so that malformed/corrupt/encrypted
    inputs surface as the documented expected exception (with the original
    preserved as ``__cause__``) instead of leaking a zoo of library-specific
    types (``BadZipFile``, ``RecursionError``, ``csv.Error`` …) that callers
    cannot catch to quarantine bad documents. Genuine programming errors are
    intentionally NOT swallowed.
    """
    try:
        yield
    except ParseError:
        raise
    except (
        zipfile.BadZipFile,
        ValueError,
        KeyError,
        OSError,
        RecursionError,
    ) as exc:
        raise ParseError(f"{engine} failed to parse input: {exc}") from exc


def guard_input_size(data: bytes, *, max_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> bytes:
    """Reject oversized raw inputs before a parser materializes them further."""
    if len(data) > max_bytes:
        raise ParseError(f"Input size {len(data)} exceeds max_input_bytes {max_bytes}")
    return data


def open_guarded_zip(data: bytes) -> zipfile.ZipFile:
    """Open a zip container after rejecting decompression-bomb shapes.

    EPUB/DOCX/PPTX/XLSX are all zip containers. Without member-count, total
    uncompressed-size, and per-member ratio checks a few-KB upload can inflate
    to gigabytes in memory. This validates the central directory *before* any
    member is read.
    """
    archive = zipfile.ZipFile(BytesIO(data))
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        archive.close()
        raise ParseError(f"Archive has {len(infos)} members (max {MAX_ARCHIVE_MEMBERS})")
    total = 0
    for info in infos:
        total += info.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            archive.close()
            raise ParseError(
                f"Archive uncompressed size exceeds {MAX_ARCHIVE_UNCOMPRESSED_BYTES} bytes"
            )
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                archive.close()
                raise ParseError(
                    f"Archive member {info.filename!r} compression ratio "
                    f"{ratio:.0f} exceeds {MAX_ARCHIVE_COMPRESSION_RATIO}"
                )
        elif info.file_size > 0:
            # compress_size == 0 with a non-zero claimed uncompressed size is
            # an effectively infinite ratio (a handful of stored/degenerate
            # bytes inflating to arbitrary size) and is itself a classic
            # zip-bomb signature, so it's rejected outright rather than
            # silently skipping the ratio check as before.
            archive.close()
            raise ParseError(
                f"Archive member {info.filename!r} claims {info.file_size} "
                "uncompressed bytes from 0 compressed bytes"
            )
    # NOTE: this trusts the zip central directory's `file_size`/
    # `compress_size` fields rather than independently re-verifying
    # decompressed bytes -- investigated as a possible bypass (a central
    # directory that under-reports these fields while the real stream
    # yields more), but it does not hold up: CPython's zipfile.ZipExtFile
    # physically bounds every read to `min(compress_size, file_size)`
    # (`ZipExtFile._read1` reads at most `compress_size` compressed bytes
    # and slices decompressed output to `self._left`, seeded from
    # `file_size`), so no consumer going through the standard zipfile API
    # (openpyxl, python-pptx, or this module's own `archive.read()`) can
    # ever extract more bytes than the declared metadata allows, forged or
    # not -- under-reporting only truncates output and fails its own CRC
    # check. A genuinely oversized member is instead caught above by the
    # total/ratio checks against its (necessarily accurate, since forging
    # it down just self-truncates) declared size.
    return archive


def raise_if_password_protected_document(
    data: bytes,
    *,
    format_name: str,
    archive: zipfile.ZipFile | None = None,
) -> None:
    """Identify supported encrypted office containers before parser libraries run.

    Password-protected OOXML documents are OLE compound files containing the
    ``EncryptionInfo`` and ``EncryptedPackage`` streams. ODT encryption stays
    inside a ZIP container and is declared in ``META-INF/manifest.xml``. Some
    producers also use the ZIP encryption flag, which is checked for both
    formats.
    """

    normalized_format = format_name.lower().strip()
    if data.startswith(_OLE_COMPOUND_FILE_SIGNATURE):
        if normalized_format == "docx" and all(
            stream_name in data for stream_name in _OOXML_ENCRYPTION_STREAMS
        ):
            raise PasswordProtectedError("DOCX is password-protected")
        return

    if archive is None:
        return
    if any(info.flag_bits & 0x1 for info in archive.infolist()):
        raise PasswordProtectedError(f"{normalized_format.upper()} is password-protected")
    if normalized_format != "odt":
        return
    try:
        manifest = archive.read("META-INF/manifest.xml")
    except KeyError:
        return
    if b"encryption-data" in manifest:
        raise PasswordProtectedError("ODT is password-protected")
