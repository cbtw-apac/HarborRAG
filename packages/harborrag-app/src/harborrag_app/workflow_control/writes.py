"""Control-plane write use cases (ML2): sources CRUD, secrets, activity logging.

Split out of client.py to keep that file under the repo's file-length gate;
mixed into AppService, which supplies the concrete _control_plane().
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeGuard
from uuid import uuid4

from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.source_config import SourceConfig
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.providers import (
    SECRET_CONFIG_FIELDS,
    ConnectorConfigFactory,
    canonical_provider_name,
    config_factory,
    config_field_names,
)

from .schemas import AppResponse

_MUTABLE_SOURCE_FIELDS = frozenset({"name", "config", "schedule", "status"})


class ControlPlaneWritesMixin:
    """Write-side control-plane use cases shared by AppService."""

    def _control_plane(self) -> ControlPlaneRepositories:
        raise NotImplementedError

    async def create_source(
        self,
        *,
        project_id: str,
        source_type: str,
        name: str,
        config: Mapping[str, Any],
        schedule: str | None,
        actor: str,
    ) -> AppResponse:
        """Create a source; secret-shaped config fields are extracted up front."""
        control_plane = self._control_plane()
        if await control_plane.projects.get(project_id) is None:
            raise HarborNotFoundError(f"project {project_id!r} not found")
        factory = _require_connector_factory(source_type)
        resolved_config, secret_refs = await _extract_secrets(control_plane, factory, config)
        source = SourceConfig(
            id=f"src_{uuid4().hex}",
            project_id=project_id,
            source_type=source_type,
            name=name,
            config=resolved_config,
            schedule=schedule,
            secret_refs=secret_refs,
        )
        created = await control_plane.sources.create(source)
        await _log_activity(
            control_plane,
            actor,
            "created",
            "source",
            created.id,
            f"Created source {created.name!r}",
        )
        return AppResponse(True, {"source": created})

    async def update_source(
        self,
        source_id: str,
        *,
        updates: dict[str, Any],
        actor: str,
    ) -> AppResponse:
        """Apply a partial update; only keys present in ``updates`` change."""
        unknown = set(updates) - _MUTABLE_SOURCE_FIELDS
        if unknown:
            raise HarborValidationError(f"unsupported source fields: {sorted(unknown)}")
        control_plane = self._control_plane()
        source = await control_plane.sources.get(source_id)
        if source is None:
            raise HarborNotFoundError(f"source {source_id!r} not found")
        if "name" in updates:
            source.name = updates["name"]
        if "schedule" in updates:
            source.schedule = updates["schedule"]
        if "status" in updates:
            source.status = updates["status"]
        if "config" in updates:
            factory = _require_connector_factory(source.source_type)
            resolved_config, secret_refs = await _extract_secrets(
                control_plane, factory, updates["config"], existing=source.config
            )
            source.config = resolved_config
            source.secret_refs = secret_refs
        updated = await control_plane.sources.update(source)
        await _log_activity(
            control_plane,
            actor,
            "updated",
            "source",
            updated.id,
            f"Updated source {updated.name!r}",
        )
        return AppResponse(True, {"source": updated})

    async def delete_source(self, source_id: str, *, actor: str) -> AppResponse:
        """Delete a source and forget every secret it referenced."""
        control_plane = self._control_plane()
        source = await control_plane.sources.get(source_id)
        if source is None:
            raise HarborNotFoundError(f"source {source_id!r} not found")
        for ref in source.secret_refs:
            await control_plane.secrets.delete(ref)
        await control_plane.sources.delete(source_id)
        await _log_activity(
            control_plane,
            actor,
            "deleted",
            "source",
            source_id,
            f"Deleted source {source.name!r}",
        )
        return AppResponse(True, {"source_id": source_id})


def _require_connector_factory(source_type: str) -> ConnectorConfigFactory:
    """Look up the provider config factory, or 501 for an unimplemented type."""
    factory = config_factory(canonical_provider_name(source_type))
    if factory is None:
        raise HarborCapabilityError(f"source_type {source_type!r} is not supported")
    return factory


async def _extract_secrets(
    control_plane: ControlPlaneRepositories,
    factory: ConnectorConfigFactory,
    incoming: Mapping[str, Any],
    *,
    existing: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Merge ``incoming`` onto ``existing`` and extract secret-field values.

    Fields absent from ``incoming`` keep their existing (already ref-shaped)
    value untouched -- this is what lets a client update one config field
    without re-sending every other one, including secret fields the API layer
    never reveals on read (SourceOut.from_domain redacts anything under a
    sensitive-looking key, so a client can't legitimately echo back a ref it
    was never shown). A secret field that IS present in ``incoming`` and
    happens to match the existing ref (the plan doc §8.2 "unchanged ref means
    keep" case) is also left alone; any other value is a new raw secret,
    stored via secrets.put(), retiring the ref it replaces via secrets.delete().
    On create, ``existing`` is None and every incoming secret field is new.
    """
    valid_fields = config_field_names(factory)
    unknown = set(incoming) - valid_fields
    if unknown:
        raise HarborValidationError(f"unsupported config fields: {sorted(unknown)}")
    merged: dict[str, Any] = {**(existing or {}), **incoming}
    secret_fields = SECRET_CONFIG_FIELDS & valid_fields
    secret_refs: list[str] = []
    for field_name in secret_fields:
        if field_name not in merged:
            continue
        value = merged[field_name]
        existing_value = (existing or {}).get(field_name)
        unset_by_caller = field_name not in incoming
        if unset_by_caller or _is_unchanged_ref(value, existing_value):
            if _is_secret_ref_shape(value):
                secret_refs.append(value["secret_ref"])
            continue
        if _is_secret_ref_shape(value):
            raise HarborValidationError(
                f"config field {field_name!r} carries an unrecognized secret_ref"
            )
        ref = await control_plane.secrets.put(str(value))
        if _is_secret_ref_shape(existing_value):
            await control_plane.secrets.delete(existing_value["secret_ref"])
        merged[field_name] = {"secret_ref": ref}
        secret_refs.append(ref)
    return merged, secret_refs


def _is_secret_ref_shape(value: object) -> TypeGuard[dict[str, str]]:
    return isinstance(value, dict) and set(value) == {"secret_ref"}


def _is_unchanged_ref(value: object, existing_value: object) -> bool:
    if not _is_secret_ref_shape(value) or not _is_secret_ref_shape(existing_value):
        return False
    return value["secret_ref"] == existing_value["secret_ref"]


async def _log_activity(
    control_plane: ControlPlaneRepositories,
    actor: str,
    verb: str,
    entity_type: str,
    entity_id: str,
    summary: str,
) -> None:
    """Append one audit row; config values are already ref-shaped by this
    point, so summaries built from name/id alone never carry secrets."""
    await control_plane.activity.append(
        ActivityEntry(
            id=f"act_{uuid4().hex}",
            actor=actor,
            verb=verb,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
        )
    )
