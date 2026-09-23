"""PendingControlPlaneEffect: durable intent for replayable cross-store work.

Secret retirement and activity logging enqueue failed post-commit effects.
Memory erasure records intent before deleting canonical identifiers so an
index outage or interrupted session purge cannot lose its retry handle. The recovery drain
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
