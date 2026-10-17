"""Access to data derived from several documents (summaries, graph views, entities).

Derived data is only as readable as its least readable input, so it is served only
to a context that could read every input: every input class, and every datasource
the inputs came from.
"""

from __future__ import annotations

from collections.abc import Iterable

from harborrag_core.security.classes import QUARANTINE_CLASS, ClassId
from harborrag_core.security.context import AccessContext


def classes_of(inputs: Iterable[ClassId]) -> frozenset[ClassId]:
    """The distinct ACL classes of the inputs a derived artifact was built from."""

    return frozenset(inputs)


def may_serve(
    access: AccessContext, *, classes: frozenset[ClassId], source_scopes: frozenset[str]
) -> bool:
    """Whether derived data built from ``classes`` and ``source_scopes`` may be served.

    - Only a ``serve`` context is ever served: a serve check refuses a ``process``
      context (see :class:`~harborrag_core.security.context.AccessContext`).
    - No input classes means no evidence of what the data was built from, and a
      quarantined input is never readable: both are ``False``.
    - When the context's ``entitled_source_scopes`` is not ``None``, every source
      scope must be entitled. With no source scopes the entitlement cannot be
      checked, so the data is not served; an empty entitlement serves nothing.
    - A ``tenant_shared`` corpus then serves the data. Otherwise the context must
      belong to every input class: ``classes <= access.class_ids``.
    """

    if access.purpose != "serve":
        return False
    if not classes or QUARANTINE_CLASS in classes:
        return False
    entitled = access.entitled_source_scopes
    if entitled is not None and not (source_scopes and source_scopes <= entitled):
        return False
    if access.corpus_mode == "tenant_shared":
        return True
    return classes <= access.class_ids
