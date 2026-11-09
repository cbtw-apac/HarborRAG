"""Request and response bodies of the ``/v1/admin/settings`` routes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SettingsOut(BaseModel):
    """Settings at one level and the revision they were read at."""

    level: str = Field(description="tenant, project:<id> or connection:<project_id>/<id>")
    revision: str = Field(description="Tenant revision, rev_<N>; also the ETag")
    values: dict[str, Any] = Field(
        description="Overrides by key; {value, origin} by key when effective=true"
    )


class SettingChangeOut(BaseModel):
    """One override change of a PATCH."""

    model_config = ConfigDict(populate_by_name=True)

    key: str
    from_value: Any = Field(alias="from", description="Effective value at the level before")
    to: Any = Field(description="New override; null when removed")
    applies: str = Field(description="live, next_ingestion or reindex")


class SettingsPatchOut(BaseModel):
    """Outcome of a PATCH: the revision after it and what changed."""

    revision: str
    dry_run: bool
    changed: list[SettingChangeOut]
