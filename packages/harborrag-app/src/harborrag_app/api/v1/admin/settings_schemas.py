"""Request and response models of `/v1/admin/settings[/schema]`."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from harborrag_app.api.schemas import ApiModel

ScopeLevelName = Literal["tenant", "project", "connection"]
SettingTypeName = Literal["string", "integer", "number", "boolean", "enum", "model_ref", "array"]
ApplyModeName = Literal["live", "next_ingestion", "reindex"]


class SettingSchemaOut(ApiModel):
    """One key's declared shape, from `SETTING_REGISTRY`."""

    key: str
    type: SettingTypeName
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple[Any, ...] | None = None
    items_type: SettingTypeName | None = None
    levels: list[ScopeLevelName]
    applies: ApplyModeName
    description: str


class SettingGroupOut(ApiModel):
    """One stage's settings, as `GET /v1/admin/settings/schema` groups them."""

    name: str
    title: str
    settings: list[SettingSchemaOut]


class SettingsSchemaOut(ApiModel):
    """`GET /v1/admin/settings/schema` response body."""

    groups: list[SettingGroupOut]


class EffectiveValueOut(ApiModel):
    """One key's resolved value plus where it came from, for `effective=true`."""

    value: Any
    origin: Literal["default", "tenant", "project", "connection"]


class SettingsReadOut(ApiModel):
    """`GET /v1/admin/settings` response body (`effective=false`): raw overrides."""

    level: str
    revision: str
    values: dict[str, Any]


class SettingsEffectiveReadOut(ApiModel):
    """`GET /v1/admin/settings?effective=true` response body."""

    level: str
    revision: str
    values: dict[str, EffectiveValueOut]


class SettingsChangeOut(ApiModel):
    """One key's before/after in a `PATCH /v1/admin/settings` response."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    key: str
    before: Any = Field(alias="from", serialization_alias="from")
    after: Any = Field(alias="to", serialization_alias="to")
    applies: ApplyModeName


class SettingsChangeOutEnvelope(ApiModel):
    """`PATCH /v1/admin/settings` response body."""

    revision: str
    dry_run: bool
    changed: list[SettingsChangeOut]
