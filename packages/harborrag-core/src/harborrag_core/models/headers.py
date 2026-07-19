from __future__ import annotations

from typing import Any

from pydantic import SecretStr

type ModelHeaderValue = str | SecretStr


def protect_model_headers(value: Any) -> Any:
    """Wrap credential-bearing request headers before Pydantic stores them."""
    if not isinstance(value, dict):
        return value
    sensitive = {"authorization", "proxy-authorization", "x-api-key", "api-key"}
    return {
        key: (SecretStr(item) if key.lower() in sensitive and isinstance(item, str) else item)
        for key, item in value.items()
    }
