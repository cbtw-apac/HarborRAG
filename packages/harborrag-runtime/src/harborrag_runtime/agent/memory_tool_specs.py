"""Tool schemas for the agent's conversation-memory surface.

Kept beside ``tool_specs.py`` rather than inside it because these two tools
differ from the retrieval ones in a way worth keeping visible: they take **no
owner fields at all**. Tenant, user, and session are bound server-side from
the authenticated run, so the schemas here have nothing for a model to spoof,
and ``additionalProperties: False`` makes a spoof attempt a validation error
rather than a silently ignored key.
"""

from __future__ import annotations

from harborrag_core.ports.memory import MemoryScope, MemoryType

from .memory_tools import READABLE_SCOPES, WRITABLE_SCOPES
from .tool_specs import RuntimeAgentToolSpec

SEARCH_MEMORY_TOOL = "search_memory"
MANAGE_MEMORY_TOOL = "manage_memory"

# The scopes a caller can *read* for itself. RUN and GLOBAL are absent on
# purpose: a run scope dies with the run, and nothing a single tenant's agent
# says may become a fact about every tenant.
_READ_SCOPES = tuple(scope.value for scope in READABLE_SCOPES)

# What it may *write* is strictly narrower: TENANT is readable but not
# writable, because one user's session stating a fact about the whole
# organization is a different trust decision from it recording a fact about
# itself -- and since a requested scope only ever narrows, an agent that could
# ask for TENANT would always get it.
_WRITE_SCOPES = tuple(scope.value for scope in WRITABLE_SCOPES)

# Durable statements only. CONVERSATION, EPISODE, SUMMARY, and WORKING are
# written by the memory layer itself from real exchanges; letting a model
# fabricate them would corrupt history rather than add to knowledge.
_TYPES = (
    MemoryType.FACT.value,
    MemoryType.PREFERENCE.value,
    MemoryType.DECISION.value,
)

_MAX_SEARCH_RESULTS = 10
_MAX_CONTENT = 2_000

SEARCH_MEMORY_DESCRIPTION = (
    "Recall what is already remembered about the person you are acting for: durable "
    "facts, preferences, and decisions from earlier sessions, ranked by relevance, "
    "recency, and importance. Use it before asking the user something they may have "
    "already told you. Results carry entity_ids that are knowledge-graph node keys, so "
    "a recalled memory can be followed into the graph tools."
)
MANAGE_MEMORY_DESCRIPTION = (
    "Record one durable fact, preference, or decision so later sessions can recall it. "
    "Only for statements that stay true beyond this conversation -- not for restating "
    "what is already in this conversation, and never for anything the user asked you to "
    "keep to this session. Recording is add-only: an existing identical memory in the "
    "same scope is kept rather than duplicated."
)


def search_memory_schema(*, max_results: int = _MAX_SEARCH_RESULTS) -> dict[str, object]:
    """Schema for recalling the acting caller's own long-term memories."""

    return {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": _MAX_CONTENT},
            "scope": {
                "type": "string",
                "enum": list(_READ_SCOPES),
                "description": (
                    "Narrow recall to one scope. Omit to search every scope the caller "
                    "can address, narrowest first."
                ),
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": max_results, "default": 5},
        },
        "additionalProperties": False,
    }


def manage_memory_schema() -> dict[str, object]:
    """Schema for recording one durable memory for the acting caller."""

    return {
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {"type": "string", "minLength": 1, "maxLength": _MAX_CONTENT},
            "memory_type": {
                "type": "string",
                "enum": list(_TYPES),
                "default": MemoryType.FACT.value,
            },
            "scope": {
                "type": "string",
                "enum": list(_WRITE_SCOPES),
                "default": MemoryScope.USER.value,
                "description": (
                    "How broadly the fact applies. Degrades to the narrowest scope the "
                    "caller can actually address rather than widening. Tenant-wide facts "
                    "are not writable here."
                ),
            },
            "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
        },
        "additionalProperties": False,
    }


MEMORY_AGENT_TOOL_SPECS = (
    RuntimeAgentToolSpec(
        SEARCH_MEMORY_TOOL,
        SEARCH_MEMORY_DESCRIPTION,
        search_memory_schema(),
    ),
    # ``write``, so the engine's read-only tool filter excludes it: registering it
    # advertises nothing today and executes nothing today. It becomes reachable
    # only when an agent run is given a write capability, which is a deliberate
    # future change to the engine's filter -- not something this module weakens.
    RuntimeAgentToolSpec(
        MANAGE_MEMORY_TOOL,
        MANAGE_MEMORY_DESCRIPTION,
        manage_memory_schema(),
        capability="write",
    ),
)

__all__ = [
    "MANAGE_MEMORY_DESCRIPTION",
    "MANAGE_MEMORY_TOOL",
    "MEMORY_AGENT_TOOL_SPECS",
    "SEARCH_MEMORY_DESCRIPTION",
    "SEARCH_MEMORY_TOOL",
    "manage_memory_schema",
    "search_memory_schema",
]
