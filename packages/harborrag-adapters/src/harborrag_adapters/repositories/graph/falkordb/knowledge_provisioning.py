"""Idempotent schema provisioning for the FalkorDB knowledge graph."""

from __future__ import annotations

import re

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (
    RELATION_IDENTIFIERS,
)

# Indexed properties track the predicates the queries actually filter on. tenant_id leads
# because every read, delete, and count scopes by it. owner_id is deliberately absent: it
# duplicates tenant_id and appears in no query, serving only the writer's ownership
# assertion.
_INDEXED_PROPERTIES = (
    "tenant_id",
    "node_key",
    "graph_schema_version",
    "entity_type",
    "ownership_scope",
    "source_scope_id",
    "document_id",
    "document_version_id",
)
# Relationship predicates were previously unindexed scans in every traversal, delete, and
# count. Indexing tenant_id per relationship type covers the universal filter.
_INDEXED_RELATION_PROPERTIES = ("tenant_id",)
# The merge identity for a node is the full triple, so the uniqueness constraint must be
# the same triple. node_key alone would both contradict the tenant merge key and reject a
# future schema version writing the same key.
_NODE_CONSTRAINT_PROPERTIES = ("node_key", "graph_schema_version", "tenant_id")
# The pre-tenancy constraint keyed node_key alone. It has to be dropped, not merely
# superseded: left in place it rejects the second tenant of a shared node_key, which is
# exactly the write the tenant merge key exists to allow.
_LEGACY_NODE_CONSTRAINT_PROPERTIES = ("node_key",)
# FalkorDB reports idempotent DDL as "already indexed" / "... already exists". Anchoring
# "already" to the word that follows is the point: a bare "exist" substring also matches
# "index does not exist", which is a real failure that must propagate. The separator is
# left loose ("already-exists", "AlreadyExists") so a provider reword cannot turn a
# re-provision into a startup failure.
_ALREADY_EXISTS = re.compile(r"already[\s_-]*(exists|indexed)", re.IGNORECASE)
# Dropping a schema object that was never created is the steady state after the first
# migration, so absence is success here. This must stay a separate matcher from
# _ALREADY_EXISTS, which deliberately refuses to treat "does not exist" as benign.
_DOES_NOT_EXIST = re.compile(
    r"(does[\s_-]*not[\s_-]*exist|not[\s_-]*found|no[\s_-]*such)", re.IGNORECASE
)


def is_already_exists_error(error: Exception) -> bool:
    """Report whether an error means the schema object was already provisioned."""

    return bool(_ALREADY_EXISTS.search(str(error)))


def is_missing_schema_object_error(error: Exception) -> bool:
    """Report whether an error means the schema object was already absent."""

    return bool(_DOES_NOT_EXIST.search(str(error)))


async def provision_graph(database: FalkorDBClient) -> None:
    """Create exact indexes and the native tenant-scoped uniqueness constraint."""

    for property_name in _INDEXED_PROPERTIES:
        await _create_index(database, property_name)
    for relationship_type in sorted(set(RELATION_IDENTIFIERS.values())):
        for property_name in _INDEXED_RELATION_PROPERTIES:
            await _create_relation_index(database, relationship_type, property_name)
    # Drop before create, never the reverse. The tenant-keyed MERGE is already live by the
    # time this runs, so leaving UNIQUE(node_key) in place rejects the second tenant of a
    # shared node_key. Creating first and dropping second would leave exactly that state
    # behind whenever the create raises for any reason other than "already exists".
    try:
        await database.drop_unique_node_constraint(
            label="KnowledgeNode",
            properties=_LEGACY_NODE_CONSTRAINT_PROPERTIES,
        )
    except Exception as exc:
        if not is_missing_schema_object_error(exc):
            raise
    try:
        await database.create_unique_node_constraint(
            label="KnowledgeNode",
            properties=_NODE_CONSTRAINT_PROPERTIES,
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


async def _create_relation_index(
    database: FalkorDBClient,
    relationship_type: str,
    property_name: str,
) -> None:
    try:
        await database.write(
            f"CREATE INDEX FOR ()-[relation:{relationship_type}]-() ON (relation.{property_name})",
            {},
        )
    except Exception as exc:
        if not is_already_exists_error(exc):
            raise
