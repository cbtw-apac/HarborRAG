"""Idempotent schema provisioning for the FalkorDB knowledge graph."""

from __future__ import annotations

import re

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient

_INDEXED_PROPERTIES = (
    "node_key",
    "document_id",
    "document_version_id",
    "source_scope_id",
)
# FalkorDB reports idempotent DDL as "already indexed" / "... already exists". Anchoring
# "already" to the word that follows is the point: a bare "exist" substring also matches
# "index does not exist", which is a real failure that must propagate. The separator is
# left loose ("already-exists", "AlreadyExists") so a provider reword cannot turn a
# re-provision into a startup failure.
_ALREADY_EXISTS = re.compile(r"already[\s_-]*(exists|indexed)", re.IGNORECASE)


def is_already_exists_error(error: Exception) -> bool:
    """Report whether an error means the schema object was already provisioned."""

    return bool(_ALREADY_EXISTS.search(str(error)))


async def provision_graph(database: FalkorDBClient) -> None:
    """Create exact indexes and the native node-key uniqueness constraint."""

    for property_name in _INDEXED_PROPERTIES:
        await _create_index(database, property_name)
    try:
        await database.create_unique_node_constraint(
            label="KnowledgeNode",
            properties=("node_key",),
        )
    except Exception as exc:
        if not is_already_exists_error(exc):
            raise


async def _create_index(database: FalkorDBClient, property_name: str) -> None:
    try:
        await database.write(
            f"CREATE INDEX FOR (node:KnowledgeNode) ON (node.{property_name})",
            {},
        )
    except Exception as exc:
        if not is_already_exists_error(exc):
            raise
