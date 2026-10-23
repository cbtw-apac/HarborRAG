"""The one reading of a candidate's stable source anchors.

``section_anchors`` feeds two identity paths that have to agree: the chunk's
own ``section_id`` (assigned in :mod:`..chunking.pipeline.result`) and the
ancestry the canonical record carries (built in :mod:`..chunking.records`).
They were derived by two near-identical private helpers, and the copies had
drifted: only one required an anchor per path segment. A connector-supplied tab
path adds a label with no source heading ID, so for those documents one helper
anchored the ID and the other did not, and the graph gained two SECTION nodes
for one section -- a split subtree no traversal could cross. One function, read
once, is what keeps the two sides from diverging again.
"""

from __future__ import annotations

from .schemas import ChunkCandidate


def section_anchors(candidate: ChunkCandidate) -> tuple[str, ...]:
    """Stable per-segment source anchors, or none when the path is not fully covered.

    Anchors are positional: ``anchors[:depth]`` must describe
    ``structural_path[:depth]``. A partial list would silently shift, so a list
    that does not cover the whole path is no list at all.
    """

    values = candidate.metadata.get("heading_element_ids")
    if not isinstance(values, (list, tuple)):
        return ()
    anchors = tuple(str(value).strip() for value in values)
    if len(anchors) != len(candidate.structural_path) or not all(anchors):
        return ()
    return anchors


__all__ = ["section_anchors"]
