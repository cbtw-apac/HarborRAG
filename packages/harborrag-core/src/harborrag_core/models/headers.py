from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, SecretStr

type ModelHeaderValue = str | SecretStr

_SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key"})


def _protect_model_headers(value: Any) -> Any:
    """Wrap credential-bearing request headers before Pydantic stores them."""
    if not isinstance(value, dict):
        return value
    return {
        key: (
            SecretStr(item) if key.lower() in _SENSITIVE_HEADERS and isinstance(item, str) else item
        )
        for key, item in value.items()
    }


type ProtectedModelHeaders = Annotated[
    dict[str, ModelHeaderValue],
    BeforeValidator(_protect_model_headers),
]
