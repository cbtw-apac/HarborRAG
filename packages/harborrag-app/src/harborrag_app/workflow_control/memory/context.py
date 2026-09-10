"""Memory-context request construction and the degraded fallback.

Chat and agent turns ask the runtime for the same thing -- one assembled
context keyed by the caller's identity -- and both must answer even when the
memory layer is unavailable. Keeping the request shape and the empty context
here means the two surfaces can never drift into asking for different things
or degrading differently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_runtime.memory import MemoryContext, MemoryContextRequest

if TYPE_CHECKING:
    from .identity import MemoryIdentity


def memory_context_request(identity: MemoryIdentity, question: str) -> MemoryContextRequest:
    """The runtime memory request for one turn of ``identity``'s conversation."""

    return MemoryContextRequest(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        user_id=identity.user_id,
        session_id=identity.session_id,
        question=question,
        project_id=identity.project_id,
    )


def empty_memory_context(question: str) -> MemoryContext:
    """The no-history context a failed memory lookup degrades to."""

    return MemoryContext(
        messages=(),
        summary=None,
        recalled=(),
        standalone_query=question,
        rewritten=False,
        summary_written=False,
    )


__all__ = ["empty_memory_context", "memory_context_request"]
