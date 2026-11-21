from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, SecretStr

type ModelHeaderValue = str | SecretStr

_SENSITIVE_HEADER_FRAGMENTS = ("key", "token", "auth", "secret", "signature", "cookie")


def is_sensitive_header(name: str) -> bool:
    """Return whether a header name suggests a credential, such as ``X-Goog-Api-Key``."""
    lowered = name.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_HEADER_FRAGMENTS)


def _protect_model_headers(value: Any) -> Any:
    """Wrap credential-bearing request headers before Pydantic stores them."""
    if not isinstance(value, dict):
        return value
    return {
        key: (SecretStr(item) if is_sensitive_header(key) and isinstance(item, str) else item)
        for key, item in value.items()
    }


type ProtectedModelHeaders = Annotated[
    dict[str, ModelHeaderValue],
    BeforeValidator(_protect_model_headers),
]
