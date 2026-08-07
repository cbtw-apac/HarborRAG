from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from harborrag_adapters.parsers.common.models import ParseRequest
from harborrag_adapters.parsers.errors import TextDecodingError
from harborrag_core.domain import ParseInput, ParserFormat

_MAX_PATH_STRING_LENGTH = 260

# charset_normalizer scores every single-byte codepage guess as low-chaos,
# since those codepages accept virtually any byte value: a handful of
# corrupted bytes inside an otherwise-ASCII document gets the same "confident"
# score as a genuine legacy-encoded document. Chaos/coherence can't tell them
# apart, so we gate on how much of the document is actually non-ASCII: a real
# Cyrillic/Greek/etc. document is overwhelmingly non-ASCII, while corrupted
# UTF-8 is overwhelmingly ASCII with a few stray bad bytes.
_MIN_NON_ASCII_RATIO_FOR_LEGACY_ENCODING = 0.1

# cp1250 (Central European) and cp1252 (Western European / "Latin-1"-ish) map
# the ASCII range identically and both accept almost any byte in 0x80-0xFF,
# so charset_normalizer frequently scores a genuine cp1252 document as an
# exact tie against cp1250 and its internal tie-break doesn't reliably land
# on cp1252 -- the far more common legacy default. This is scoped tightly to
# that specific confusion pair (not "any single-byte codepage") because
# forcing cp1252 as a candidate for an unrelated script (Cyrillic, Greek,
# Baltic, ...) can tie or win on chaos while being a worse decode overall;
# limiting the re-check to documents charset_normalizer already called
# cp1250 avoids ever touching those.
_CP1250_CP1252_CONFUSION_PAIR = ("cp1250", "cp1252")


def coerce_parse_input(
    value: Any,
    *,
    allow_path_strings: bool = False,
) -> ParseInput:
    """Normalize adapter inputs without placing filesystem policy in core."""
    if isinstance(value, ParseInput):
        return value
    if isinstance(value, bytes):
        return ParseInput(content=value)
    if isinstance(value, Path):
        return ParseInput(path=value)
    if isinstance(value, str):
        path = _existing_path(value) if allow_path_strings else None
        return ParseInput(path=path) if path is not None else ParseInput(content=value)

    text_method = getattr(value, "text", None)
    content = getattr(value, "content", None)
    if content is None and callable(text_method):
        content = text_method()
    metadata = getattr(value, "metadata", {}) or {}
    return ParseInput(
        path=getattr(value, "path", None),
        content=content,
        filename=(
            getattr(value, "filename", None)
            or getattr(value, "file_name", None)
            or _name_from(getattr(value, "source", None))
            or _name_from(getattr(value, "source_id", None))
        ),
        content_type=getattr(value, "content_type", None),
        metadata=dict(metadata),
    )


def parse_input_suffix(value: ParseInput) -> str:
    """Return the normalized filename suffix used for parser routing."""
    candidate = value.filename or value.path
    return Path(candidate).suffix.lower() if candidate else ""


def request_to_parse_input(request: ParseRequest) -> ParseInput:
    content = request.options.get("content")
    if content is not None and not isinstance(content, (bytes, str)):
        raise TypeError("ParseRequest options.content must be bytes or text")

    parsed_uri = urlparse(request.source_uri)
    path: Path | None = None
    if content is None and parsed_uri.scheme == "file":
        path = Path(unquote(parsed_uri.path))
    elif content is None and not parsed_uri.scheme:
        path = Path(request.source_uri)
    elif content is None:
        raise ValueError("Remote ParseRequest sources require caller-provided options.content")

    metadata = request.options.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("ParseRequest options.metadata must be a mapping")
    return ParseInput(
        path=path,
        content=content,
        filename=request.filename,
        content_type=request.mime_type,
        metadata=dict(metadata),
    )


def read_parse_input_bytes(value: ParseInput) -> bytes:
    """Load parser bytes at the adapter boundary."""
    if isinstance(value.content, bytes):
        return value.content
    if isinstance(value.content, str):
        return value.content.encode("utf-8")
    if value.path is not None:
        return Path(value.path).read_bytes()
    raise ValueError("ParseInput has no readable bytes")


def read_parse_input_text(value: ParseInput, encoding: str | None = None) -> str:
    """Decode parser input with deterministic BOM handling.

    Text-based formats (Markdown, HTML, JSON, ...) are a deterministic UTF
    input boundary: falling back to a statistical single-byte detector (e.g.
    `charset_normalizer`) when UTF-8 decoding fails can turn corrupt UTF-8
    into valid-looking but incorrect text (commonly Cyrillic CP1251, since it
    maps every byte value) instead of surfacing the corruption. Callers
    should catch `UnicodeDecodeError` and raise a typed parse error.
    """
    if isinstance(value.content, str):
        return value.content
    data = read_parse_input_bytes(value)
    if encoding:
        return _decode_with_explicit_encoding(data, encoding)
    bom_text = _decode_bom(data)
    if bom_text is not None:
        return bom_text
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return _decode_with_legacy_detection(data)


def _decode_with_explicit_encoding(data: bytes, encoding: str) -> str:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as error:
        raise TextDecodingError(byte_length=len(data), guessed_encoding=encoding) from error


def _decode_bom(data: bytes) -> str | None:
    """Decode a BOM-declared encoding, if present. Returns None if absent/undecodable."""
    for candidate in ("utf-8-sig", "utf-16"):
        if _has_bom(data, candidate):
            try:
                return data.decode(candidate)
            except UnicodeDecodeError:
                return None
    return None


def _decode_with_legacy_detection(data: bytes) -> str:
    """Fall back to charset_normalizer, but only trust guesses with enough signal.

    Multi-byte guesses (UTF-16/UTF-32/Shift-JIS/...) are trusted outright:
    their code units impose enough structure that a confident match on
    corrupted-but-mostly-ASCII bytes practically never happens. Single-byte
    codepage guesses (cp1251, cp1250, ...) get no such structural check for
    free -- see :func:`_is_plausible_legacy_encoding` -- so they're gated on
    non-ASCII byte density instead. Before that gate runs, a cp1250 guess is
    specifically re-checked against cp1252 -- see
    :func:`_resolve_cp1250_cp1252_confusion`.

    Raises :class:`TextDecodingError` instead of returning an unreliable guess.
    """
    guessed_encoding: str | None = None
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        best = _resolve_cp1250_cp1252_confusion(data, best)
        if best is not None:
            guessed_encoding = str(best.encoding)
            if best.multi_byte_usage or _is_plausible_legacy_encoding(data):
                return str(best)
    except ImportError:
        pass
    raise TextDecodingError(byte_length=len(data), guessed_encoding=guessed_encoding)


def _resolve_cp1250_cp1252_confusion(data: bytes, best: Any) -> Any:
    """Prefer cp1252 over a cp1250 guess when the two are an actual tie.

    Only triggers when charset_normalizer's unconstrained guess is cp1250:
    genuine cp1252 documents that get mis-guessed land there, and this never
    fires for any other codepage, so unrelated scripts (Cyrillic, Greek,
    Baltic, ...) are never re-scored against cp1252. Swaps in cp1252 only
    when it scores no worse on both chaos and coherence -- genuine cp1250
    text (Polish, Czech, ...) scores strictly worse under cp1252 and is left
    untouched.
    """
    if best is None or best.encoding not in _CP1250_CP1252_CONFUSION_PAIR:
        return best
    if best.encoding == "cp1252":
        return best
    from charset_normalizer import from_bytes

    preferred = from_bytes(data, cp_isolation=["cp1252"]).best()
    if (
        preferred is not None
        and preferred.chaos <= best.chaos
        and preferred.coherence >= best.coherence
    ):
        return preferred
    return best


def _is_plausible_legacy_encoding(data: bytes) -> bool:
    """Reject low-signal encoding guesses for mostly-ASCII corrupted UTF-8.

    Single-byte legacy codepages (cp1251, cp1252, ...) accept almost any byte
    value, so charset_normalizer reports the same low-chaos "confident" score
    for a genuine legacy-encoded document as it does for a handful of invalid
    bytes sitting inside otherwise-valid ASCII text. A real legacy-encoded
    document is overwhelmingly non-ASCII; corrupted UTF-8 is not.
    """
    if not data:
        return False
    non_ascii = sum(1 for byte in data if byte >= 0x80)
    return (non_ascii / len(data)) >= _MIN_NON_ASCII_RATIO_FOR_LEGACY_ENCODING


def parse_input_supports(
    value: ParseInput,
    supported_formats: Iterable[ParserFormat | str],
) -> bool:
    """Return whether a parser input matches any declared format."""
    suffix = parse_input_suffix(value).lstrip(".")
    content_type = (value.content_type or "").lower()
    return any(
        _format_value(format_).lower() == suffix or _format_value(format_).lower() in content_type
        for format_ in supported_formats
    )


def _format_value(value: ParserFormat | str) -> str:
    return value.value if isinstance(value, ParserFormat) else str(value)


def _has_bom(data: bytes, encoding: str) -> bool:
    if encoding == "utf-8-sig":
        return data.startswith(b"\xef\xbb\xbf")
    if encoding == "utf-16":
        return data.startswith((b"\xff\xfe", b"\xfe\xff"))
    return False


def _name_from(value: Any) -> str | None:
    if not value:
        return None
    return Path(str(value)).name or None


def _existing_path(value: str) -> Path | None:
    if "\n" in value or len(value) >= _MAX_PATH_STRING_LENGTH:
        return None
    try:
        path = Path(value)
    except OSError:
        return None
    return path if path.exists() else None
