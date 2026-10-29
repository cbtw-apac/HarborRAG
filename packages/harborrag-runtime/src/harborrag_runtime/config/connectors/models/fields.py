"""Field types shared by the source-type models.

Handles:
- ``Secret``: a write-only credential that is a value or a secret reference
- ``HttpUrl`` and ``Email``: string fields with a published JSON Schema format
- widget markers that turn into ``ui`` hints for the connection form
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema
from pydantic import EmailStr as _EmailStr
from pydantic.fields import FieldInfo

Widget = Literal["text", "secret", "select", "textarea", "path"]

WIDGET_MARKER = "x-widget"
_HTTP_URL = re.compile(r"^https?://[^\s/]+\S*$")


class StrictModel(BaseModel):
    """Base model for credentials, config and tuning: unknown fields are errors."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SecretReference(BaseModel):
    """A stored or external secret pointer; extra keys carry the stored status."""

    model_config = ConfigDict(extra="allow")

    secret_ref: str = Field(min_length=1)


class SecretMarker:
    """Annotation metadata that flags a field as a write-only secret."""


def _require_http_url(value: str) -> str:
    """Reject anything that is not an absolute http(s) URL."""
    if not _HTTP_URL.match(value):
        raise ValueError("must be an absolute http(s) URL")
    return value.rstrip("/")


Secret = Annotated[
    str | SecretReference,
    SecretMarker(),
    WithJsonSchema({"type": "string", "writeOnly": True}),
]
HttpUrl = Annotated[
    str,
    AfterValidator(_require_http_url),
    WithJsonSchema({"type": "string", "format": "uri"}),
]
Email = Annotated[_EmailStr, WithJsonSchema({"type": "string", "format": "email"})]


def widget(kind: Widget, **extra: Any) -> dict[str, Any]:
    """Return ``json_schema_extra`` that pins the form widget of a field."""
    return {WIDGET_MARKER: kind, **extra}


def is_secret(field: FieldInfo) -> bool:
    """True when ``field`` was declared with the ``Secret`` type, optional or not."""
    if any(isinstance(item, SecretMarker) for item in field.metadata):
        return True
    return _carries_marker(field.annotation)


def _carries_marker(annotation: Any) -> bool:
    """Search ``annotation`` and its union members for ``SecretMarker`` metadata."""
    if get_origin(annotation) is Annotated:
        return any(isinstance(item, SecretMarker) for item in annotation.__metadata__)
    return any(_carries_marker(member) for member in get_args(annotation))
