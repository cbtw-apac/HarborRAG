"""Shared constants and row access for the FalkorDB knowledge-graph modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborDeadlineExceeded
from harborrag_core.ingestion import GraphOwnershipScope, KnowledgeNodeKind
from harborrag_core.retrieval import GraphAccessScope

NODE_LABELS = {
    KnowledgeNodeKind.TENANT: "Tenant",
    KnowledgeNodeKind.DATA_SOURCE: "DataSource",
    KnowledgeNodeKind.SOURCE_ENTITY: "SourceEntity",
    KnowledgeNodeKind.DOCUMENT_VERSION: "DocumentVersion",
    KnowledgeNodeKind.STRUCTURE: "Structure",
    KnowledgeNodeKind.CHUNK: "Chunk",
}
RELATION_IDENTIFIERS = {
    relation_type: FalkorDBMapper.safe_identifier(relation_type.value)
    for relation_type in RelationType
}

# A bounded traversal asks for more paths than nodes because one path contributes several
# nodes and many paths revisit nodes already seen. Four paths per requested node keeps the
# read wide enough to fill max_nodes after deduplication without unbounded fan-out.
PATH_LIMIT_FACTOR = 4


def path_limit_for(max_nodes: int) -> int:
    """Return how many paths to read to satisfy a node-bounded traversal."""

    return max_nodes * PATH_LIMIT_FACTOR


def access_predicate(variable: str) -> str:
    """Return the fixed Cypher ACL applied before ordering and limiting graph rows."""

    return f"""(
        $access_unrestricted
        OR ({variable}.document_id IS NOT NULL
            AND {variable}.document_id IN $authorized_document_ids)
        OR ({variable}.document_id IS NULL
            AND {variable}.source_scope_id IS NOT NULL
            AND {variable}.source_scope_id IN $authorized_source_scope_ids)
        OR ({variable}.document_id IS NULL
            AND {variable}.source_scope_id IS NULL
            AND {variable}.ownership_scope = $tenant_ownership_scope
            AND $tenant_visible)
    )"""


def access_parameters(scope: GraphAccessScope | None) -> dict[str, Any]:
    """Bind one access scope for ``access_predicate``; a missing scope denies everything.

    ``None`` used to bind ``access_unrestricted``, so any query whose scope was never
    set, or was dropped by ``model_dump`` (the field is excluded), read the whole
    tenant. Unrestricted is now asked for by name: ``GraphAccessScope.allow_all()``.
    The ownership value is bound rather than inlined so it cannot drift from the
    enum the writes store -- an inlined ``'tenant'`` never matched ``'TENANT'``.
    """

    effective = scope if scope is not None else GraphAccessScope.deny_all()
    return {
        "access_unrestricted": effective.unrestricted or effective.tenant_shared,
        "authorized_document_ids": list(effective.document_ids),
        "authorized_source_scope_ids": list(effective.source_scope_ids),
        "tenant_visible": effective.tenant_visible,
        "tenant_ownership_scope": GraphOwnershipScope.TENANT.value,
    }


async def read_rows(
    database: FalkorDBClient,
    statement: str,
    parameters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Execute a read statement and decode it into plain row mappings."""

    return FalkorDBMapper.rows(await database.read(statement, parameters))


# FalkorDB aborts a read that outlives its server-side TIMEOUT with a bare ResponseError
# carrying exactly this text; the exception type is redis's generic one, so the message is
# the only thing that distinguishes "too expensive" from a malformed query.
_QUERY_TIMEOUT_MESSAGE = "query timed out"


async def read_retrieval_rows(
    database: FalkorDBClient,
    statement: str,
    parameters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """``read_rows`` for interactive retrieval, with a server timeout made explicit.

    A timed-out traversal is the caller's bounds being too wide for the neighborhood,
    not a broken backend, and a reader tool can only say so if it can tell the two
    apart. Translating it here keeps the provider's error text inside the adapter, and
    only for retrieval: ingestion reads share ``read_rows`` but not this translation,
    because ingestion policy treats ``HarborDeadlineExceeded`` as transient and would
    start retrying reads whose failure classification has not changed.
    """

    try:
        return await read_rows(database, statement, parameters)
    except Exception as exc:
        if _QUERY_TIMEOUT_MESSAGE in str(exc).casefold():
            raise HarborDeadlineExceeded("graph query timed out") from exc
        raise


async def relationship_types_matching(
    database: FalkorDBClient,
    predicate: str,
    parameters: Mapping[str, Any],
) -> tuple[str, ...]:
    """The knowledge relationship types holding at least one ``relation`` that matches.

    An untyped ``()-[relation]->()`` pattern can use no relationship index, so it walks
    every edge in the graph -- once per document at ingestion, which makes a large
    source quadratic. Asking each type in one read lets every branch use that type's
    index, and the writes that follow touch only the types that hold something.
    """

    branches = [
        f"MATCH ()-[relation:{identifier}]->() WHERE {predicate} "
        f"RETURN '{identifier}' AS relationship_type, count(relation) AS relations"
        for identifier in sorted(set(RELATION_IDENTIFIERS.values()))
    ]
    rows = await read_rows(database, "\nUNION ALL\n".join(branches), parameters)
    return tuple(sorted(str(row["relationship_type"]) for row in rows if int(row["relations"]) > 0))
