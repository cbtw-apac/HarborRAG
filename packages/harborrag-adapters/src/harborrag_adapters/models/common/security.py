from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_REDACTED = "**********"
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "credential",
    "cookie",
)


class SecretReference(BaseModel):
    """Reference external secret material without storing it in configuration output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=10, examples=["secret://vault/harborrag/model-key"])

    @field_validator("uri")
    @classmethod
    def require_secret_scheme(cls, value: str) -> str:
        """Reject ambiguous references that are not explicitly secret URIs."""

        if not value.startswith("secret://") or not value.removeprefix("secret://"):
            raise ValueError("secret reference URI must start with secret://")
        return value

    def __repr__(self) -> str:
        """Render without exposing the secret location."""

        return f"SecretReference(uri={_REDACTED!r})"

    def __str__(self) -> str:
        """Render without exposing the secret location."""

        return _REDACTED


type SecretValue = SecretStr | SecretReference
type HeaderValue = str | SecretStr | SecretReference


class SecretResolver(Protocol):
    """Resolve a secret reference at an application-controlled boundary."""

    def resolve(self, reference: SecretReference) -> str:
        """Return plaintext for the supplied reference."""

        ...


class PrivacyConfig(BaseModel):
    """Control sanitization of request, response, and configuration data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    log_inputs: bool = False
    log_outputs: bool = False
    hash_user_identifiers: bool = True
    redact_fields: frozenset[str] = frozenset(
        {
            "api_key",
            "authorization",
            "password",
            "secret",
            "token",
            "retrieval_query",
        }
    )
    metadata_allowlist: frozenset[str] = frozenset(
        {
            "request_id",
            "trace_id",
            "tenant_id",
            "user_id",
            "workflow_id",
            "collection_name",
            "pipeline_stage",
            "embedding_purpose",
            "conversation_id",
            "prompt_template_version",
        }
    )
    max_logged_content_length: int = Field(default=2_000, ge=0, le=100_000)


class PrivacySanitizer:
    """Redact, truncate, and hash sensitive telemetry values according to policy."""

    def __init__(self, config: PrivacyConfig) -> None:
        """Store the privacy policy used to sanitize values on demand."""

        self.config = config

    def sanitize(self, value: Any, *, field_name: str | None = None) -> Any:
        """Recursively redact secrets, denylisted fields, and long strings."""

        if isinstance(value, (SecretStr, SecretReference)):
            return _REDACTED
        if field_name and _is_sensitive_field(field_name, self.config.redact_fields):
            return "[REDACTED]"
        if isinstance(value, BaseModel):
            return {
                name: self.sanitize(getattr(value, name), field_name=name)
                for name in type(value).model_fields
            }
        if isinstance(value, Mapping):
            return {
                str(key): self.sanitize(item, field_name=str(key)) for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.sanitize(item) for item in value]
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, str):
            if field_name == "arguments":
                parsed = _json_container(value)
                if parsed is not None:
                    return json.dumps(self.sanitize(parsed), separators=(",", ":"), default=str)
            return value[: self.config.max_logged_content_length]
        return value

    def metadata(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Filter metadata to the allowlist and hash selected identifiers."""

        filtered = {
            key: item for key, item in value.items() if key in self.config.metadata_allowlist
        }
        if self.config.hash_user_identifiers:
            for key in ("user_id", "tenant_id"):
                identifier = filtered.get(key)
                if identifier:
                    filtered[key] = hashlib.sha256(str(identifier).encode()).hexdigest()
        sanitized = self.sanitize(filtered)
        return sanitized if isinstance(sanitized, dict) else {}

    def identifier(self, value: str | None) -> str | None:
        """Hash an identifier when configured, otherwise return it unchanged."""

        if value is None or not self.config.hash_user_identifiers:
            return value
        return hashlib.sha256(value.encode()).hexdigest()

    def content(self, value: Any) -> Any:
        """Sanitize and bound a prompt or response payload by serialized length."""

        sanitized = self.sanitize(value)
        limit = self.config.max_logged_content_length
        if limit == 0:
            return None
        try:
            encoded = json.dumps(sanitized, sort_keys=True, default=str)
        except (TypeError, ValueError):
            encoded = str(sanitized)
        return sanitized if len(encoded) <= limit else encoded[:limit]


def resolve_secret_references(value: Any, resolver: SecretResolver | None) -> Any:
    """Materialize secret URIs and optionally resolve them before validation."""

    if isinstance(value, SecretReference) and resolver is None:
        return value
    reference = _secret_reference(value)
    if reference is not None:
        if resolver is None:
            return {"uri": reference.uri}
        return SecretStr(resolver.resolve(reference))
    if isinstance(value, Mapping):
        return {key: resolve_secret_references(item, resolver) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_secret_references(item, resolver) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_secret_references(item, resolver) for item in value)
    return value


def reveal_secret(value: SecretValue | HeaderValue | None) -> str | None:
    """Return resolved plaintext, rejecting unresolved external references."""

    if value is None:
        return None
    if isinstance(value, SecretReference):
        raise ValueError(f"unresolved secret reference: {value.uri}")
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def sanitize_configuration(value: BaseModel) -> dict[str, Any]:
    """Return JSON-compatible configuration data with all sensitive values redacted."""

    sanitized = PrivacySanitizer(PrivacyConfig()).sanitize(value)
    return sanitized if isinstance(sanitized, dict) else {}


def _secret_reference(value: Any) -> SecretReference | None:
    if isinstance(value, SecretReference):
        return value
    if isinstance(value, str) and value.startswith("secret://"):
        return SecretReference(uri=value)
    if (
        isinstance(value, Mapping)
        and set(value) == {"uri"}
        and str(value.get("uri", "")).startswith("secret://")
    ):
        return SecretReference.model_validate(value)
    return None


def _is_sensitive_field(field_name: str, configured_fields: frozenset[str]) -> bool:
    lowered = field_name.lower()
    return (
        lowered in configured_fields
        or lowered.endswith("_token")
        or lowered.startswith("token_")
        or any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)
    )


def _json_container(value: str) -> dict[str, Any] | list[Any] | None:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None
