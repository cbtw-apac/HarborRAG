"""Control-plane write use cases (ML2): sources CRUD with secrets handling.

Split out as a sibling of reads.py, mixed into AppService, which supplies
the concrete _control_plane(). Secret-shaped config fields never round-trip
through the route layer -- see _extract_secrets.

Every write here follows the primary source row's commit with one or two
secondary effects (stale-secret retirement, audit logging) that must never
turn a successful write into an API error. ``_retire_refs``/``_log_activity``
therefore never raise: a failed attempt is durably queued via
``pending_effects`` instead, and ``recover_pending_control_plane_effects``
retries it later. Both replayed actions are safe to retry blindly -- secret
deletion is a no-op on an already-gone ref, and a requeued activity entry
keeps its original id, so it can't double-write.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypeGuard
from uuid import uuid4

from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.providers import (
    SECRET_CONFIG_FIELDS,
    ConnectorConfigFactory,
    canonical_provider_name,
    config_factory,
    config_field_names,
)

from ..schemas import AppResponse

logger = logging.getLogger("harborrag.app.workflow_control.control_plane.writes")

_MUTABLE_SOURCE_FIELDS = frozenset({"name", "config", "schedule", "status"})
_RETIRE_SECRET_KIND = "retire_secret"
_LOG_ACTIVITY_KIND = "log_activity"


class ControlPlaneWritesMixin:
    """Write-side control-plane use cases shared by AppService."""

    def _control_plane(self) -> ControlPlaneRepositories:
        raise NotImplementedError

    async def create_source(  # noqa: PLR0913 - explicit source-creation fields keep secret handling auditable
        self,
        *,
        tenant_id: str,
        project_id: str,
        source_type: str,
        name: str,
        config: Mapping[str, Any],
        schedule: str | None,
        actor: str,
    ) -> AppResponse:
        """Create a source; secret-shaped config fields are extracted up front."""
        control_plane = self._control_plane()
        tenant_ids = frozenset({tenant_id})
        if await control_plane.projects.get(project_id, tenant_ids=tenant_ids) is None:
            raise HarborNotFoundError(f"project {project_id!r} not found")
        factory = _require_connector_factory(source_type)
        resolved_config, secret_refs, newly_put_refs, _stale = await _extract_secrets(
            control_plane, factory, config
        )
        source = SourceConfig(
            id=f"src_{uuid4().hex}",
            tenant_id=tenant_id,
            project_id=project_id,
            source_type=source_type,
            name=name,
            config=resolved_config,
            schedule=schedule,
            secret_refs=secret_refs,
        )
        try:
            created = await control_plane.sources.create(source)
        except Exception:
            await _retire_refs(control_plane, newly_put_refs)
            raise
        await _log_activity(
            control_plane,
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=tenant_id,
                actor=actor,
                verb="created",
                entity_type="source",
                entity_id=created.id,
                summary=f"Created source {created.name!r}",
            ),
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
        source = await control_plane.sources.get(source_id, tenant_ids=None)
        if source is None:
            raise HarborNotFoundError(f"source {source_id!r} not found")
        if "name" in updates:
            source.name = updates["name"]
        if "schedule" in updates:
            source.schedule = updates["schedule"]
        if "status" in updates:
            source.status = updates["status"]
        newly_put_refs: list[str] = []
        stale_refs: list[str] = []
        if "config" in updates:
            factory = _require_connector_factory(source.source_type)
            resolved_config, secret_refs, newly_put_refs, stale_refs = await _extract_secrets(
                control_plane, factory, updates["config"], existing=source.config
            )
            source.config = resolved_config
            source.secret_refs = secret_refs
        try:
            updated = await control_plane.sources.update(source)
        except Exception:
            # The source row never picked up the new refs -- retire them so they
            # don't linger as orphaned secrets pointing at nothing.
            await _retire_refs(control_plane, newly_put_refs)
            raise
        # Only now that the source row durably references the new refs is it
        # safe to retire the old ones -- doing this before the write above
        # could leave a persisted source pointing at an already-deleted secret.
        await _retire_refs(control_plane, stale_refs)
        await _log_activity(
            control_plane,
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=updated.tenant_id,
                actor=actor,
                verb="updated",
                entity_type="source",
                entity_id=updated.id,
                summary=f"Updated source {updated.name!r}",
            ),
        )
        return AppResponse(True, {"source": updated})

    async def delete_source(self, source_id: str, *, actor: str) -> AppResponse:
        """Delete a source and forget every secret it referenced."""
        control_plane = self._control_plane()
        source = await control_plane.sources.get(source_id, tenant_ids=None)
        if source is None:
            raise HarborNotFoundError(f"source {source_id!r} not found")
        await control_plane.sources.delete(source_id, tenant_ids=None)
        await _retire_refs(control_plane, source.secret_refs)
        await _log_activity(
            control_plane,
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=source.tenant_id,
                actor=actor,
                verb="deleted",
                entity_type="source",
                entity_id=source_id,
                summary=f"Deleted source {source.name!r}",
            ),
        )
        return AppResponse(True, {"source_id": source_id})

    async def recover_pending_control_plane_effects(self, *, limit: int = 100) -> int:
        """Retry durably-queued secret retirements and audit-log writes.

        Each pending row's first attempt ran only after the write it depends
        on already committed, so retrying is always safe and never touches
        an in-flight request. A row that fails again is left pending for the
        next drain pass; one bad row must not block the rest.
        """
        control_plane = self._control_plane()
        recovered = 0
        for effect in await control_plane.pending_effects.list_pending(limit=limit):
            try:
                await _replay_effect(control_plane, effect)
            except Exception:
                logger.exception(
                    "control-plane pending effect retry failed id=%s kind=%s",
                    effect.id,
                    effect.kind,
                )
                continue
            await control_plane.pending_effects.complete(effect.id)
            recovered += 1
        return recovered


def _require_connector_factory(source_type: str) -> ConnectorConfigFactory:
    """Look up the provider config factory, or 501 for an unimplemented type."""
    factory = config_factory(canonical_provider_name(source_type))
    if factory is None:
        raise HarborCapabilityError(f"source_type {source_type!r} is not supported")
    return factory


async def _retire_refs(control_plane: ControlPlaneRepositories, refs: list[str]) -> None:
    """Delete each ref; a failed delete is queued for retry, never raised."""
    for ref in refs:
        try:
            await control_plane.secrets.delete(ref)
        except Exception:
            logger.exception("secret retirement failed ref=%r; queued for retry", ref)
            await _queue_effect(control_plane, _RETIRE_SECRET_KIND, {"ref": ref})


async def _extract_secrets(
    control_plane: ControlPlaneRepositories,
    factory: ConnectorConfigFactory,
    incoming: Mapping[str, Any],
    *,
    existing: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Merge ``incoming`` onto ``existing`` and extract secret-field values.

    Fields absent from ``incoming`` keep their existing (already ref-shaped)
    value untouched -- this is what lets a client update one config field
    without re-sending every other one, including secret fields the API layer
    never reveals on read. A secret field that IS present in ``incoming`` and
    happens to match the existing ref is also left alone; any other value is
    a new raw secret, stored via secrets.put(). On create, ``existing`` is
    None and every incoming secret field is new.

    Returns (merged_config, secret_refs, newly_put_refs, stale_refs). This
    function never deletes a ref itself: the caller must delete
    ``stale_refs`` only after the source row's write commits, and delete
    ``newly_put_refs`` instead if that write fails -- deleting a stale ref
    up front would leave a persisted source pointing at nothing if the write
    later fails.
    """
    valid_fields = config_field_names(factory)
    unknown = set(incoming) - valid_fields
    if unknown:
        raise HarborValidationError(f"unsupported config fields: {sorted(unknown)}")
    merged: dict[str, Any] = {**(existing or {}), **incoming}
    secret_fields = SECRET_CONFIG_FIELDS & valid_fields
    secret_refs: list[str] = []
    newly_put_refs: list[str] = []
    stale_refs: list[str] = []
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
        if not isinstance(value, str):
            raise HarborValidationError(
                f"config field {field_name!r} must contain a string secret value"
            )
        ref = await control_plane.secrets.put(value)
        newly_put_refs.append(ref)
        if _is_secret_ref_shape(existing_value):
            stale_refs.append(existing_value["secret_ref"])
        merged[field_name] = {"secret_ref": ref}
        secret_refs.append(ref)
    return merged, secret_refs, newly_put_refs, stale_refs


def _is_secret_ref_shape(value: object) -> TypeGuard[Mapping[str, str]]:
    return isinstance(value, Mapping) and set(value) == {"secret_ref"}


def _is_unchanged_ref(value: object, existing_value: object) -> bool:
    return (
        _is_secret_ref_shape(value)
        and _is_secret_ref_shape(existing_value)
        and value["secret_ref"] == existing_value["secret_ref"]
    )


async def _log_activity(control_plane: ControlPlaneRepositories, entry: ActivityEntry) -> None:
    """Append one audit row; a failed append is queued for retry, never raised."""
    try:
        await control_plane.activity.append(entry)
    except Exception:
        logger.exception("activity logging failed entry_id=%r; queued for retry", entry.id)
        await _queue_effect(control_plane, _LOG_ACTIVITY_KIND, _activity_payload(entry))


async def _queue_effect(
    control_plane: ControlPlaneRepositories, kind: str, payload: dict[str, Any]
) -> None:
    """Durably record a failed side effect; logged (not raised) if even this fails.

    Both call sites here run after the primary write they depend on already
    committed, so there is no error left to surface to the caller -- a
    failure enqueuing the retry itself just means this side effect is lost
    until someone notices the log line.
    """
    try:
        await control_plane.pending_effects.enqueue(
            PendingControlPlaneEffect(id=f"eff_{uuid4().hex}", kind=kind, payload=payload)
        )
    except Exception:
        logger.critical(
            "control-plane pending effect enqueue failed kind=%s payload=%r; effect lost",
            kind,
            payload,
            exc_info=True,
        )


async def _replay_effect(
    control_plane: ControlPlaneRepositories, effect: PendingControlPlaneEffect
) -> None:
    if effect.kind == _RETIRE_SECRET_KIND:
        await control_plane.secrets.delete(effect.payload["ref"])
        return
    if effect.kind == _LOG_ACTIVITY_KIND:
        await control_plane.activity.append(_activity_from_payload(effect.payload))
        return
    raise HarborInvariantError(f"unknown pending control-plane effect kind: {effect.kind!r}")


def _activity_payload(entry: ActivityEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "actor": entry.actor,
        "verb": entry.verb,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "summary": entry.summary,
        "tenant_id": entry.tenant_id,
        "created_at": entry.created_at.isoformat(),
    }


def _activity_from_payload(payload: dict[str, Any]) -> ActivityEntry:
    return ActivityEntry(
        id=payload["id"],
        actor=payload["actor"],
        verb=payload["verb"],
        entity_type=payload["entity_type"],
        entity_id=payload["entity_id"],
        summary=payload["summary"],
        tenant_id=payload["tenant_id"],
        created_at=datetime.fromisoformat(payload["created_at"]),
    )
