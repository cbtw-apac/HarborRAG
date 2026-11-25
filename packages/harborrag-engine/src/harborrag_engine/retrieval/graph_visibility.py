"""Permission filtering and connectivity repair for structural graph reads."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from harborrag_core.contracts.errors import (
    HarborAuthorizationUnavailableError,
    HarborConflictError,
)
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, GraphOwnershipScope
from harborrag_core.retrieval import GraphAccessScope, GraphDirection
from harborrag_core.retrieval.graph import GraphDocumentAccess
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext

from .graph_metadata import selector_matches

logger = logging.getLogger("harborrag.engine.retrieval.graph_visibility")

# ``external_source_entity_node_key``'s domain prefix. The key, not the ``external``
# attribute, marks a stub here: a shared source entity is stored without attributes,
# so the flag does not survive a read back from the graph store.
_EXTERNAL_STUB_KEY_PREFIX = "graph-v2-external-source-entity:"
_ENUMERATION_BUDGET = 10_000
_NODE_KEY = re.compile(r"(?:graph-v2-[a-z]+(?:-[a-z]+)*|chunk):[0-9a-f]{64}")


class GraphVisibilityAuthorizer(Protocol):
    """Resolve graph ownership scopes against canonical reader permissions."""

    async def allowed_document_ids(
        self,
        tenant_id: str,
        *,
        access: AccessContext,
        limit: int = 10000,
    ) -> tuple[str, ...]: ...

    async def allowed_source_scope_ids(
        self,
        tenant_id: str,
        *,
        access: AccessContext,
        limit: int = 10000,
    ) -> tuple[str, ...]: ...

    async def authorized_document_ids(
        self,
        tenant_id: str,
        document_ids: tuple[str, ...],
        *,
        access: AccessContext,
    ) -> set[str]: ...

    async def authorized_source_scope_ids(
        self,
        tenant_id: str,
        source_scope_ids: tuple[str, ...],
        *,
        access: AccessContext,
    ) -> set[str]: ...


async def graph_access_scope(
    authorizer: GraphVisibilityAuthorizer | None,
    context: StorageOperationContext,
) -> GraphAccessScope | None:
    """Resolve canonical allowlists before the graph store selects or limits rows.

    The readable documents themselves, while there are few enough to list. Past the
    enumeration budget the readable source scopes stand in, as on the vector path: a
    document is readable only inside a readable source scope, so that scope admits
    everything the principal may read, and ``apply_graph_permissions`` removes the
    documents of those scopes they may not.
    """

    if authorizer is None:
        return None
    if context.access.corpus_mode == "tenant_shared":
        return GraphAccessScope(tenant_shared=True)
    tenant_id = str(context.tenant_id)
    document_ids, source_scope_ids = await asyncio.gather(
        authorizer.allowed_document_ids(
            tenant_id, access=context.access, limit=_ENUMERATION_BUDGET
        ),
        authorizer.allowed_source_scope_ids(
            tenant_id, access=context.access, limit=_ENUMERATION_BUDGET
        ),
        return_exceptions=True,
    )
    if isinstance(source_scope_ids, BaseException):
        raise source_scope_ids
    sources = tuple(sorted(set(source_scope_ids)))
    if isinstance(document_ids, HarborConflictError):
        logger.info(
            "Readable documents exceed the enumeration budget; graph pre-filtering by source",
            extra={"tenant_id": tenant_id, "source_scopes": len(sources)},
        )
        return GraphAccessScope(
            source_scope_ids=sources, document_access=GraphDocumentAccess.BY_SOURCE_SCOPE
        )
    if isinstance(document_ids, BaseException):
        raise document_ids
    return GraphAccessScope(document_ids=tuple(sorted(set(document_ids))), source_scope_ids=sources)


def store_truncated(truncated: bool, access_scope: GraphAccessScope | None) -> bool:
    """Return the store's truncation bit when only readable documents could have set it.

    Under the source-scope fallback the store selects from every document of a
    readable source, so its cut can be made by documents the caller may not read.
    Truncation then comes from the visible results alone, as on the vector path.
    """

    return truncated and (
        access_scope is None or access_scope.document_access == GraphDocumentAccess.LISTED
    )


def refuse_inexact_selector(access_scope: GraphAccessScope | None) -> None:
    """Refuse a selector the store would resolve by logical id or title past the budget.

    Under the source-scope fallback the store resolves a selector among every document
    of a readable source. A logical-id or title match in a document the caller may not
    read can then become the anchor, stop resolution at its tier or fill the candidate
    cap, so whether anything comes back would depend on that document. The vector path
    refuses a ``fields.*`` filter in the same state for the same reason.

    Raises:
        HarborAuthorizationUnavailableError: ``access_scope`` is the source-scope fallback.
    """

    if (
        access_scope is not None
        and access_scope.document_access == GraphDocumentAccess.BY_SOURCE_SCOPE
    ):
        logger.info("Graph selector refused: readable documents exceed the enumeration budget")
        raise HarborAuthorizationUnavailableError("AUTHORIZATION_UNAVAILABLE")


def require_node_key_selectors(
    access_scope: GraphAccessScope | None, selectors: Iterable[str | None]
) -> None:
    """Refuse, past the budget, any selector that is not a node key or chunk id.

    The store tries a selector as a node key first, then as a logical id and a title.
    Only the node-key form names one node, so only it keeps resolving under the
    source-scope fallback.

    Raises:
        HarborAuthorizationUnavailableError: A selector is not a node key under the
            source-scope fallback.
    """

    if any(selector is not None and not _NODE_KEY.fullmatch(selector) for selector in selectors):
        refuse_inexact_selector(access_scope)


async def apply_graph_permissions(
    records: Mapping[str, GraphNodeRecord | GraphEdgeRecord],
    states: dict[str, str],
    authorizer: GraphVisibilityAuthorizer | None,
    context: StorageOperationContext,
) -> dict[str, str]:
    """Mark inaccessible records without exposing denied counts to diagnostics."""

    if authorizer is None:
        return states
    document_ids = tuple(
        dict.fromkeys(
            str(record.document_id) for record in records.values() if record.document_id is not None
        )
    )
    source_ids = tuple(
        dict.fromkeys(
            record.source_scope_id
            for record in records.values()
            if record.source_scope_id is not None
        )
    )
    allowed_documents, allowed_sources = await asyncio.gather(
        authorizer.authorized_document_ids(
            str(context.tenant_id), document_ids, access=context.access
        ),
        authorizer.authorized_source_scope_ids(
            str(context.tenant_id), source_ids, access=context.access
        ),
    )
    # A tenant-scoped record carries no document and no source to check, so
    # whether it is readable is a question about the *principal*, not about
    # whatever else happened to be retrieved beside it. Resolve it from the
    # principal's own allowlists, and only when the batch actually contains
    # such a record.
    tenant_visible = False
    if any(
        (record.document_id is None and record.source_scope_id is None) or _is_external_stub(record)
        for record in records.values()
    ):
        tenant_visible = await _tenant_readable(authorizer, context)
    for key, record in records.items():
        if not _record_authorized(record, allowed_documents, allowed_sources, tenant_visible):
            states[key] = "denied"
    return states


def reachable_subgraph(
    nodes: Sequence[GraphNodeRecord],
    relations: Sequence[GraphEdgeRecord],
    *,
    selector: str,
    direction: GraphDirection,
    max_nodes: int,
) -> tuple[tuple[GraphNodeRecord, ...], tuple[GraphEdgeRecord, ...], int]:
    """Keep only the visible neighborhood still reachable without hidden intermediates."""

    seed = next((node for node in nodes if selector_matches(node, selector)), None)
    if seed is None:
        return (), (), 0
    node_by_key = {node.node_key: node for node in nodes}
    order = {node.node_key: index for index, node in enumerate(nodes)}
    neighbors: dict[str, set[str]] = {key: set() for key in node_by_key}
    for relation in relations:
        source = relation.source_node_key
        target = relation.target_node_key
        if source not in node_by_key or target not in node_by_key:
            continue
        if direction in {GraphDirection.OUTGOING, GraphDirection.BOTH}:
            neighbors[source].add(target)
        if direction in {GraphDirection.INCOMING, GraphDirection.BOTH}:
            neighbors[target].add(source)
    reached: list[str] = []
    queued = {seed.node_key}
    frontier = [seed.node_key]
    while frontier:
        current = frontier.pop(0)
        reached.append(current)
        for neighbor in sorted(neighbors[current], key=order.__getitem__):
            if neighbor not in queued:
                queued.add(neighbor)
                frontier.append(neighbor)
    selected_keys = set(reached[:max_nodes])
    selected_nodes = tuple(node_by_key[key] for key in reached[:max_nodes])
    selected_relations = tuple(
        relation
        for relation in relations
        if relation.source_node_key in selected_keys and relation.target_node_key in selected_keys
    )
    return selected_nodes, selected_relations, len(reached)


async def _tenant_readable(
    authorizer: GraphVisibilityAuthorizer,
    context: StorageOperationContext,
) -> bool:
    """Whether this principal may read anything at all in this tenant.

    Deriving this from the batch made the answer depend on batch composition: a
    batch holding only tenant-scoped nodes produced empty allowlists and denied
    them to a fully authorized reader, while a single readable document
    alongside them unlocked every one.

    ``limit`` on these ports is an enumeration *budget*, not a truncation: a
    reader with more readable resources than the budget gets a
    ``HarborConflictError`` rather than a short list. Asking with ``limit=1``
    therefore failed every reader holding two grants. Only the readable source
    scopes are enumerated, under the same budget :func:`graph_access_scope`
    spends on this request: a document is readable only inside a readable
    source scope, so a reader of any document holds one, and a reader of more
    documents than the budget is not refused here. In ``tenant_shared`` mode the
    whole tenant is readable by construction and no allowlist is consulted.
    """

    if context.access.corpus_mode == "tenant_shared":
        return True
    return bool(
        await authorizer.allowed_source_scope_ids(
            str(context.tenant_id), access=context.access, limit=_ENUMERATION_BUDGET
        )
    )


def _record_authorized(
    record: GraphNodeRecord | GraphEdgeRecord,
    document_ids: set[str],
    source_scope_ids: set[str],
    tenant_visible: bool,
) -> bool:
    if record.document_id is not None:
        return str(record.document_id) in document_ids
    if _is_external_stub(record):
        return tenant_visible
    if record.source_scope_id is not None:
        return record.source_scope_id in source_scope_ids
    return record.ownership_scope == GraphOwnershipScope.TENANT and tenant_visible


def _is_external_stub(record: GraphNodeRecord | GraphEdgeRecord) -> bool:
    """Whether this is the stand-in for a link target no ingested scope has published.

    Its key is scope-free, so every scope linking that item shares one stub, and the
    ``source_scope_id`` it carries is whichever declarer was projected last. Checked
    against that scope, the stub -- and every link through it -- would appear and vanish
    for a reader with ingestion order rather than with permissions. It holds a provider
    id and nothing else, and the links to it are version-owned supports checked against
    their declaring document, so it is readable by anyone who can read the tenant.
    """

    return isinstance(record, GraphNodeRecord) and (
        record.node_key.startswith(_EXTERNAL_STUB_KEY_PREFIX)
        or record.attributes.get("external") is True
    )
