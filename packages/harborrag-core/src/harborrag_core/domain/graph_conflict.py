"""GraphConflict: a queued disagreement in the knowledge graph awaiting human resolution.

v1 is record-only: ``resolve`` persists the chosen action and closes the
conflict but does not itself mutate FalkorDB -- no existing graph adapter
primitive can patch a single node/relation in place (only whole-projection
writes exist). Nothing in this codebase reports a conflict yet either;
``report`` exists for future detection code or manual seeding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ConflictAction = Literal["replace", "version", "append", "skip", "archive", "merge"]
ConflictStatus = Literal["open", "resolved"]


@dataclass(slots=True)
class GraphConflict:
    """One detected graph disagreement, open until a human picks a resolution action."""

    id: str
    tenant_id: str
    conflict_type: str
    subject_node_key: str
    description: str
    detected_at: datetime
    competing_node_key: str | None = None
    status: ConflictStatus = "open"
    action: ConflictAction | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
