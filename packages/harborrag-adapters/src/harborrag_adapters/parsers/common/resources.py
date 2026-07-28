from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from harborrag_adapters.parsers.common.models import ParseRequest
from harborrag_core.domain import ParseInput, ParserFormat

_MAX_PATH_STRING_LENGTH = 260


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
    """Decode parser input with deterministic BOM and confidence handling."""
    if isinstance(value.content, str):
        return value.content
    data = read_parse_input_bytes(value)
    if encoding:
        return data.decode(encoding)
    for candidate in ("utf-8-sig", "utf-16"):
        if _has_bom(data, candidate):
            try:
                return data.decode(candidate)
            except UnicodeDecodeError:
                break
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except ImportError:
        pass
    raise UnicodeDecodeError("utf-8", data, 0, len(data), "no encoding detected with confidence")


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
