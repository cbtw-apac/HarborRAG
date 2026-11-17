"""Canonical HarborRAG memory schema exports.

The core package owns the actual dataclasses and protocols. This module gives
``harborrag-memory`` a stable, first-class import surface for the shared memory
model without duplicating the types.
"""

from __future__ import annotations

from harborrag_core.ports.memory import (
    Memory as Memory,
)
from harborrag_core.ports.memory import (
    MemoryEmbedder as MemoryEmbedder,
)
from harborrag_core.ports.memory import (
    MemoryEntityResolver as MemoryEntityResolver,
)
from harborrag_core.ports.memory import (
    MemoryIndex as MemoryIndex,
)
from harborrag_core.ports.memory import (
    MemoryMatch as MemoryMatch,
)
from harborrag_core.ports.memory import (
    MemoryOwner as MemoryOwner,
)
from harborrag_core.ports.memory import (
    MemoryQuery as MemoryQuery,
)
from harborrag_core.ports.memory import (
    MemoryRepository as MemoryRepository,
)
from harborrag_core.ports.memory import (
    MemoryScope as MemoryScope,
)
from harborrag_core.ports.memory import (
    MemoryType as MemoryType,
)
from harborrag_core.ports.memory import (
    ResolvedEntity as ResolvedEntity,
)
from harborrag_core.ports.memory import (
    new_memory_id as new_memory_id,
)
from harborrag_core.ports.memory import (
    scope_owner_fields as scope_owner_fields,
)
from harborrag_core.ports.memory import (
    visible_to as visible_to,
)
