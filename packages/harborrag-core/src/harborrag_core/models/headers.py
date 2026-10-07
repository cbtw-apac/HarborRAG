from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, SecretStr

from harborrag_core.security.field_names import is_sensitive_field_name

type ModelHeaderValue = str | SecretStr


def _protect_model_headers(value: Any) -> Any:
    """Wrap credential-bearing request headers before Pydantic stores them.

    Which headers carry a credential is the one shared definition, so a provider's
    own key header (``x-goog-api-key``, ``ocp-apim-subscription-key``) is protected
    without a list here to keep in step. This only wraps values; it rejects nothing,
    so a benign header such as ``Idempotency-Key`` passes through unchanged.
    """

    if not isinstance(value, dict):
        return value
    return {
        key: (SecretStr(item) if isinstance(item, str) and is_sensitive_field_name(key) else item)
        for key, item in value.items()
    }


type ProtectedModelHeaders = Annotated[
    dict[str, ModelHeaderValue],
    BeforeValidator(_protect_model_headers),
]
