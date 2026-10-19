"""Topology evidence explains a result only with documents the reader may read.

A passage reached through the topology graph carries the assertions and navigation
summaries that led to it. Serving builds are already limited to a reader's entitled
datasources (``eligible_builds``), so no assertion owned by another datasource's document
should reach here. This is defence in depth: if one ever did, a route could run through
it and end at a passage the reader is entitled to. The passage would stay; the
explanation must not name the other document.
"""

from __future__ import annotations

from dataclasses import replace

from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.search import TopologyEvidence

from .permissions import RetrievalPermissions
from .topology import TopologyCandidates


async def restrict_to_readable_documents(
    batch: TopologyCandidates,
    permissions: RetrievalPermissions,
    context: StorageOperationContext,
) -> TopologyCandidates:
    """Drop the assertions, paths and navigation summaries owned by unreadable documents.

    Only a reader with a datasource entitlement set is checked, as defence in depth: the
    serving builds are already limited to that set (and, on a ``source_acl`` corpus, to
    the reader's ACLs), so the check should drop nothing. Without a set, the serving
    builds are limited to what the corpus policy lets the reader read, and the one extra
    canonical read would change nothing. A navigation summary carries no document of its
    own, so it follows the document of the passage it rides on.
    """

    if context.access.entitled_source_scopes is None or not batch.evidence:
        return batch
    documents: set[str] = set()
    for item in batch.evidence.values():
        documents.update(assertion.document_id for assertion in item.assertions)
        if item.navigation_summaries:
            documents.add(item.document_id)
    if not documents:
        return batch
    readable = await permissions.authorized_documents(tuple(sorted(documents)), context)
    return replace(
        batch,
        evidence={chunk: _readable(item, readable) for chunk, item in batch.evidence.items()},
    )


def _readable(evidence: TopologyEvidence, readable: set[str]) -> TopologyEvidence:
    kept = tuple(item for item in evidence.assertions if item.document_id in readable)
    dropped = {item.assertion_id for item in evidence.assertions} - {
        item.assertion_id for item in kept
    }
    navigation = evidence.navigation_summaries if evidence.document_id in readable else ()
    if not dropped and navigation == evidence.navigation_summaries:
        return evidence
    return replace(
        evidence,
        assertions=kept,
        assertion_ids=tuple(item for item in evidence.assertion_ids if item not in dropped),
        paths=tuple(path for path in evidence.paths if dropped.isdisjoint(path.assertion_ids)),
        navigation_summaries=navigation,
    )
