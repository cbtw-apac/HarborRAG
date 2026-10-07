"""The graph ACL is bound explicitly: a missing scope denies, unrestricted is named."""

from __future__ import annotations

from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (
    access_parameters,
    access_predicate,
)
from harborrag_core.ingestion import GraphOwnershipScope
from harborrag_core.retrieval import GraphAccessScope, GraphSubgraphQuery


def test_a_missing_scope_denies_every_record() -> None:
    assert access_parameters(None) == {
        "access_unrestricted": False,
        "authorized_document_ids": [],
        "authorized_source_scope_ids": [],
        "tenant_visible": False,
        "tenant_ownership_scope": GraphOwnershipScope.TENANT.value,
    }


def test_unrestricted_access_must_be_asked_for_by_name() -> None:
    assert access_parameters(GraphAccessScope.allow_all())["access_unrestricted"] is True
    assert access_parameters(GraphAccessScope(tenant_shared=True))["access_unrestricted"] is True
    assert access_parameters(GraphAccessScope(document_ids=("d1",)))["access_unrestricted"] is False


def test_a_scope_dropped_by_serialization_denies_everything() -> None:
    query = GraphSubgraphQuery(
        start_node="node-1", access_scope=GraphAccessScope(document_ids=("d1",))
    )

    # The field is excluded from dumps so allowlists never reach a log or a
    # response; what must change is what its absence means downstream.
    restored = GraphSubgraphQuery.model_validate(query.model_dump())

    assert restored.access_scope is None
    parameters = access_parameters(restored.access_scope)
    assert parameters["access_unrestricted"] is False
    assert parameters["tenant_visible"] is False


def test_tenant_owned_records_compare_against_the_stored_enum_value() -> None:
    predicate = access_predicate("node")

    assert "'tenant'" not in predicate
    assert "node.ownership_scope = $tenant_ownership_scope" in predicate
    assert (
        access_parameters(GraphAccessScope(document_ids=("d1",)))["tenant_ownership_scope"]
        == "TENANT"
    )
