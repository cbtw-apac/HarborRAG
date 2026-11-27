"""Durable retry queue for control-plane secret retirement and audit logging.

Connection and project writes call ``retire_refs``/``log_activity`` here instead
of touching ``control_plane.secrets``/``activity`` directly, so a failure after
their write already committed is queued for retry rather than raised -- never
turning a successful write into an API error. ``recover_pending_control_plane_effects``
drains that queue later. Both replayed actions are safe to retry blindly: secret
deletion is a no-op on an already-gone ref, and a requeued activity entry keeps
its original id, so it can't double-write.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.observability.log_events import (
    FailureKind,
    log_event,
    log_failure,
)
from harborrag_runtime.composition import ControlPlaneRepositories

logger = logging.getLogger("harborrag.runtime.application.control_plane.effect_recovery")

_RETIRE_SECRET_KIND = "retire_secret"
_RETIRE_WHEN_IDLE_KIND = "retire_secret_when_idle"
_LOG_ACTIVITY_KIND = "log_activity"
_PAYLOAD_ID_FIELDS = {
    "tenant_id": "tenant_id",
    "source_key": "connection_id",
    "id": "activity_entry_id",
}
"""Payload keys that hold IDs, and the log field each is written as; refs never are."""

type ConnectionBusy = Callable[[str, str], Awaitable[bool]]
LEGACY_SECRET_TENANT_ID = "DEFAULT"
"""Tenant migration 0032 gives a legacy secret that no source row still owns."""

EFFECT_RECOVERY_LEASE_NAME = "control_plane_effect_recovery"
"""Lease ``AppService.recover_pending_control_plane_effects`` runs its drain under.

It mirrors the ingestion progress bridge's ``LEASE_NAME``. Without it every API
process or replica drains the same queue on its own timer and can replay the same
pending effect concurrently. ``retire_secret`` is idempotent either way, but
``log_activity`` is not: two processes replaying one entry ID both attempt an insert,
and only a unique-ID constraint or the repository's idempotent-append guard stops
the double entry.
"""
EFFECT_RECOVERY_LEASE_TTL_SECONDS = 120.0
"""Lease lifetime of a few drain intervals, so a crashed holder's lease lapses quickly."""


async def retire_refs(
    control_plane: ControlPlaneRepositories,
    refs: list[str],
    *,
    tenant_id: str,
) -> None:
    """Delete each of one tenant's refs; a failed delete is queued, never raised.

    The tenant travels with the queued effect because secret refs are
    tenant-scoped: a replay that presented the ref alone would address
    nothing and silently leave the secret behind.
    """
    for ref in refs:
        try:
            await control_plane.secrets.delete(ref, tenant_id=tenant_id)
        except Exception as error:  # noqa: BLE001 - a stale ref is retried later
            log_failure(
                logger,
                "control_plane.secret_retirement_failed",
                error,
                FailureKind.UNEXPECTED,
                tenant_id=tenant_id,
            )
            await _queue_effect(
                control_plane,
                _RETIRE_SECRET_KIND,
                {"ref": ref, "tenant_id": tenant_id},
            )


async def log_activity(control_plane: ControlPlaneRepositories, entry: ActivityEntry) -> None:
    """Append one audit row; a failed append is queued for retry, never raised."""
    try:
        await control_plane.activity.append(entry)
    except Exception as error:  # noqa: BLE001 - the audit row is queued for retry
        log_failure(
            logger,
            "control_plane.activity_log_failed",
            error,
            FailureKind.UNEXPECTED,
            activity_entry_id=entry.id,
            tenant_id=entry.tenant_id,
        )
        await _queue_effect(control_plane, _LOG_ACTIVITY_KIND, _activity_payload(entry))


async def defer_retirement(
    control_plane: ControlPlaneRepositories,
    refs: list[str],
    *,
    tenant_id: str,
    source_key: str,
) -> None:
    """Queue stored refs to be deleted once no run of the connection still uses them."""
    for ref in refs:
        await _queue_effect(
            control_plane,
            _RETIRE_WHEN_IDLE_KIND,
            {"ref": ref, "tenant_id": tenant_id, "source_key": source_key},
        )


async def recover_pending_control_plane_effects(
    control_plane: ControlPlaneRepositories,
    *,
    limit: int = 100,
    erasure_handler: Callable[[PendingControlPlaneEffect], Awaitable[bool]] | None = None,
    connection_busy: ConnectionBusy | None = None,
) -> int:
    """Retry queued effects and authorized cross-store erasure intents.

    Post-commit effects and pre-delete erasure intents are idempotent; a
    concurrent initial attempt may perform the same deletion harmlessly.
    A row that fails again is left pending for the next drain pass; one bad
    row must not block the rest. A failure to delete a row that already replayed
    is logged and the row replays again next pass, which is safe because both
    effect kinds are idempotent.
    """
    recovered = 0
    for effect in await control_plane.pending_effects.list_pending(limit=limit):
        try:
            if await _still_in_use(effect, connection_busy):
                continue
            if erasure_handler is None or not await erasure_handler(effect):
                await _replay_effect(control_plane, effect)
        except Exception as error:  # noqa: BLE001 - one bad effect must not stop the drain
            log_failure(
                logger,
                "control_plane.pending_effect_retry_failed",
                error,
                FailureKind.UNEXPECTED,
                effect_id=effect.id,
                effect_kind=effect.kind,
            )
            continue
        try:
            await control_plane.pending_effects.complete(effect.id)
        except Exception as error:  # noqa: BLE001 - the effect is retried next drain
            log_failure(
                logger,
                "control_plane.pending_effect_complete_failed",
                error,
                FailureKind.UNEXPECTED,
                effect_id=effect.id,
                effect_kind=effect.kind,
            )
            continue
        recovered += 1
    return recovered


async def _queue_effect(
    control_plane: ControlPlaneRepositories, kind: str, payload: dict[str, Any]
) -> None:
    """Durably record a failed side effect; logged (not raised) if even this fails.

    Both call sites here run after the primary write they depend on already
    committed, so there is no error left to surface to the caller -- a
    failure enqueuing the retry itself just means this side effect is lost
    until someone notices the log line. That line names the effect's kind and IDs,
    never its payload, which holds secret references.
    """
    try:
        await control_plane.pending_effects.enqueue(
            PendingControlPlaneEffect(id=f"eff_{uuid4().hex}", kind=kind, payload=payload)
        )
    except Exception as error:  # noqa: BLE001 - logged as lost; the caller's change stands
        log_event(
            logger,
            "control_plane.pending_effect_lost",
            level=logging.CRITICAL,
            hint=(
                "This effect is not queued for retry; retire the secret or restore the "
                "audit entry by hand."
            ),
            effect_kind=kind,
            **_payload_ids(payload),
            error=error,
        )


def _payload_ids(payload: Mapping[str, object]) -> dict[str, object]:
    """Return the IDs a queued effect's payload holds, keyed by their log field names."""
    return {field: payload[key] for key, field in _PAYLOAD_ID_FIELDS.items() if key in payload}


async def _still_in_use(
    effect: PendingControlPlaneEffect, connection_busy: ConnectionBusy | None
) -> bool:
    """Report whether a deferred retirement's connection still has an active run."""
    if effect.kind != _RETIRE_WHEN_IDLE_KIND:
        return False
    if connection_busy is None:
        return True
    return await connection_busy(
        str(effect.payload["tenant_id"]), str(effect.payload["source_key"])
    )


async def _replay_effect(
    control_plane: ControlPlaneRepositories, effect: PendingControlPlaneEffect
) -> None:
    """Perform one queued effect.

    A secret retirement queued before refs became tenant-scoped carries no tenant. Its ref
    was retired from the source that used it, so migration 0032 found no owner and left
    the secret under ``LEGACY_SECRET_TENANT_ID``; it is retired there rather than dropped,
    which would orphan the ciphertext.

    Args:
        control_plane: The repositories the effect writes to.
        effect: The queued effect to perform.

    Raises:
        HarborInvariantError: The effect kind is unknown.
    """
    if effect.kind == _RETIRE_WHEN_IDLE_KIND:
        await control_plane.secrets.delete(
            str(effect.payload["ref"]), tenant_id=str(effect.payload["tenant_id"])
        )
        return
    if effect.kind == _RETIRE_SECRET_KIND:
        tenant_id = effect.payload.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            log_event(
                logger,
                "control_plane.secret_retirement_without_tenant",
                level=logging.WARNING,
                effect_id=effect.id,
                tenant_id=LEGACY_SECRET_TENANT_ID,
            )
            tenant_id = LEGACY_SECRET_TENANT_ID
        await control_plane.secrets.delete(str(effect.payload["ref"]), tenant_id=tenant_id)
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
