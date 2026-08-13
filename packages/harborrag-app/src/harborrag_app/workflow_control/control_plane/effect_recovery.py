"""Durable retry queue for control-plane secret retirement and audit logging.

Split out of writes.py (file-length gate) as a sibling module. writes.py's
create_source/update_source/delete_source call ``retire_refs``/``log_activity``
here instead of touching ``control_plane.secrets``/``activity`` directly, so a
failure after their write already committed is queued for retry rather than
raised -- never turning a successful write into an API error.
``recover_pending_control_plane_effects`` (delegated to from
``ControlPlaneWritesMixin``) drains that queue later. Both replayed actions
are safe to retry blindly: secret deletion is a no-op on an already-gone ref,
and a requeued activity entry keeps its original id, so it can't double-write.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.composition import ControlPlaneRepositories

logger = logging.getLogger("harborrag.app.workflow_control.control_plane.effect_recovery")

_RETIRE_SECRET_KIND = "retire_secret"
_LOG_ACTIVITY_KIND = "log_activity"

# Named lease AppService.recover_pending_control_plane_effects runs its drain
# under (LeaseRepositoryPort), mirroring the ingestion progress bridge's
# LEASE_NAME -- without it, every API process/replica drains the same queue
# on its own timer and can replay the same pending effect concurrently.
# retire_secret is idempotent either way (deleting an already-gone ref is a
# no-op), but log_activity is not: two processes racing to replay the same
# entry id both attempt an insert, and only a unique-id constraint (or the
# idempotent-append guard on the repository) stops that from double-logging.
# TTL is a few drain intervals so a crashed holder's lease lapses quickly
# without its own timer jitter costing it the lease.
EFFECT_RECOVERY_LEASE_NAME = "control_plane_effect_recovery"
EFFECT_RECOVERY_LEASE_TTL_SECONDS = 120.0


async def retire_refs(control_plane: ControlPlaneRepositories, refs: list[str]) -> None:
    """Delete each ref; a failed delete is queued for retry, never raised."""
    for ref in refs:
        try:
            await control_plane.secrets.delete(ref)
        except Exception:
            logger.exception("secret retirement failed ref=%r; queued for retry", ref)
            await _queue_effect(control_plane, _RETIRE_SECRET_KIND, {"ref": ref})


async def log_activity(control_plane: ControlPlaneRepositories, entry: ActivityEntry) -> None:
    """Append one audit row; a failed append is queued for retry, never raised."""
    try:
        await control_plane.activity.append(entry)
    except Exception:
        logger.exception("activity logging failed entry_id=%r; queued for retry", entry.id)
        await _queue_effect(control_plane, _LOG_ACTIVITY_KIND, _activity_payload(entry))


async def recover_pending_control_plane_effects(
    control_plane: ControlPlaneRepositories, *, limit: int = 100
) -> int:
    """Retry durably-queued secret retirements and audit-log writes.

    Each pending row's first attempt ran only after the write it depends on
    already committed, so retrying is always safe and never touches an
    in-flight request. A row that fails again is left pending for the next
    drain pass; one bad row must not block the rest.
    """
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
        try:
            await control_plane.pending_effects.complete(effect.id)
        except Exception:
            # The replay above already succeeded -- only the delete from the
            # queue failed. Leaving the row pending just means it replays
            # again next pass (safe: both effect kinds are idempotent), so a
            # failure here must not abort the rest of this batch.
            logger.exception(
                "control-plane pending effect complete failed id=%s kind=%s; will retry",
                effect.id,
                effect.kind,
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
