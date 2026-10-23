"""Over the enumeration budget, nothing a caller sees depends on documents they cannot read."""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest
from retrieval_test_support import policy, resources
from topology_retrieval_support import ActiveVersions, CanonicalTopology, TopologyVectors

from harborrag_core.contracts import HarborConflictError
from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
from harborrag_core.indexing import VectorFilter, VectorFilterCondition
from harborrag_core.security import AccessContext
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService

ACCESS = AccessContext(principal_id="broad-reader", tenant_id="tenant-1")


class OverBudgetTopology(CanonicalTopology):
    """The reader can read scope HR, but not document-1, which lives in it."""

    def __init__(self) -> None:
        super().__init__()
        self.readable = {"document-2"}

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        raise HarborConflictError("authorized document enumeration exceeds the configured budget")

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        return ("HR",)

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        return set(document_ids) & self.readable


class EmptyVectors(TopologyVectors):
    """The same collection with nothing that matches the query."""

    def _results(self, collection):
        return []


class FieldVectors(TopologyVectors):
    """``fields.*`` matches one item: the hidden issue behind document-1."""

    def __init__(self, items: tuple[str, ...]) -> None:
        super().__init__()
        self.items = items

    async def distinct_values(self, index_name, field, *, filters, limit, context):
        return self.items


def _service(vectors) -> RuntimeRetrievalService:
    return RuntimeRetrievalService(
        resources=replace(
            resources(vectors=vectors, active_versions=ActiveVersions()),
            topology_repository=OverBudgetTopology(),
        ),
        policy=policy(),
    )


def _visible(report) -> dict[str, object]:
    diagnostics = asdict(report.diagnostics)
    diagnostics.pop("duration_ms")
    return {"results": report.results, "diagnostics": diagnostics}


@pytest.mark.asyncio
async def test_a_hidden_chunk_is_indistinguishable_from_no_match() -> None:
    hidden = await _service(TopologyVectors()).retrieve(
        "question", tenant_id="tenant-1", access=ACCESS
    )
    nothing = await _service(EmptyVectors()).retrieve(
        "question", tenant_id="tenant-1", access=ACCESS
    )
    assert hidden.results == ()
    assert _visible(hidden) == _visible(nothing)
    assert hidden.diagnostics.candidate_hits == 0


@pytest.mark.parametrize("items", [("HIDDEN-1",), ()])
@pytest.mark.asyncio
async def test_a_fields_filter_over_the_budget_gives_one_answer_whatever_matches(items) -> None:
    fields = VectorFilter(must=[VectorFilterCondition(field="fields.assignee", value="alice")])
    with pytest.raises(HarborAuthorizationUnavailableError, match="AUTHORIZATION_UNAVAILABLE"):
        await _service(FieldVectors(items)).retrieve(
            "question",
            tenant_id="tenant-1",
            access=ACCESS,
            options=RetrievalOptions(filters=fields),
        )


@pytest.mark.asyncio
async def test_the_fields_limit_cannot_be_reached_through_unreadable_items() -> None:
    fields = VectorFilter(must=[VectorFilterCondition(field="fields.assignee", value="alice")])
    many = tuple(f"HIDDEN-{n}" for n in range(10_001))
    # Refused for being over the budget, never for "matches more than N items".
    with pytest.raises(HarborAuthorizationUnavailableError):
        await _service(FieldVectors(many)).retrieve(
            "question",
            tenant_id="tenant-1",
            access=ACCESS,
            options=RetrievalOptions(filters=fields),
        )
