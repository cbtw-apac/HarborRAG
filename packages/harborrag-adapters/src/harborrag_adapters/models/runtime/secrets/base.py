from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from ..security import SecretReference


@dataclass(frozen=True, slots=True)
class ParsedSecretReference:
    """Expose decoded provider, path segments, and optional JSON field fragment."""

    provider: str
    segments: tuple[str, ...]
    field: str | None


def parse_secret_reference(reference: SecretReference) -> ParsedSecretReference:
    """Parse one secret URI into stable decoded routing components."""

    parsed = urlsplit(reference.uri)
    if parsed.scheme != "secret" or not parsed.netloc:
        raise ValueError("secret URI must include a provider authority")
    segments = tuple(unquote(part) for part in parsed.path.split("/") if part)
    return ParsedSecretReference(
        provider=parsed.netloc.lower(),
        segments=segments,
        field=unquote(parsed.fragment) if parsed.fragment else None,
    )
