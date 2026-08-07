"""Canonical HarborRAG memory schema exports.

The core package owns the actual dataclasses and protocols. This module gives
``harborrag-memory`` a stable, first-class import surface for the shared memory
model without duplicating the types.
"""

from __future__ import annotations

from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    MemoryType,
    new_memory_id,
    scope_owner_fields,
    visible_to,
)

__all__ = [
    "Memory",
    "MemoryOwner",
    "MemoryQuery",
    "MemoryRepository",
    "MemoryScope",
    "MemoryType",
    "new_memory_id",
    "scope_owner_fields",
    "visible_to",
]