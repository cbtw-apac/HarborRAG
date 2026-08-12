"""PendingControlPlaneEffect: a durable retry record for a post-commit side effect.

Enqueued only when a secondary effect (secret retirement, activity logging)
fails after the primary control-plane write it depends on has already
committed -- never a step on the happy path. The recovery drain
(``AppService.recover_pending_control_plane_effects``) retries each row and
removes it once the retry succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from harborrag_core.base import utc_now


@dataclass(slots=True)
class PendingControlPlaneEffect:
    """One durable side effect awaiting retry, keyed by ``id`` for idempotent completion."""

    id: str
    kind: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=utc_now)
