"""Semantic expansion survives a large derived-product registry and a failing contextual lane."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from test_contextual_evidence_search import PROFILE, Vectors, manifest, query

from harborrag_core.contracts import HarborConflictError
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.permissions import DerivedArtifactRecord
from harborrag_engine.retrieval.service.contextual import ContextualEvidenceSearch
from harborrag_engine.retrieval.service.contracts import RetrievalOptions
from harborrag_engine.retrieval.service.service import RuntimeRetrievalService
from harborrag_engine.testing.retrieval_fakes import (
    ActiveVersions,
    CanonicalTopology,
    TopologyVectors,
    policy,
    resources,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CONTEXT = StorageOperationContext.system("tenant-1", idempotency_key="request-1")
LOCAL = RetrievalOptions(mode="local_semantic")


class BudgetedDerivations:
    """A derived-artifact authority that keeps the adapter's budget semantics.

    ``limit`` is an enumeration budget: more matching rows than it allows raise
    rather than truncate, and ``artifact_kind`` narrows the rows counted.
    """

    def __init__(self, records: tuple[DerivedArtifactRecord, ...]) -> None:
        self.records = records
        self.kinds: list[str | None] = []

    async def active_derivations(self, tenant_id, *, access=None, artifact_kind=None, limit=100):
        del tenant_id, access
        self.kinds.append(artifact_kind)
        rows = tuple(
            record
            for record in self.records
            if artifact_kind is None or record.lineage.artifact_kind == artifact_kind
        )
        if len(rows) > max(1, min(limit, 10000)):
            raise HarborConflictError("derived artifact enumeration exceeds bounded limit")
        return rows


def _entity_view(index: int) -> DerivedArtifactRecord:
    record = manifest()
    return record.model_copy(
        update={
            "lineage": record.lineage.model_copy(
                update={"artifact_id": f"entity-view-{index}", "artifact_kind": "entity_view"}
            )
        }
    )


async def test_contextual_search_ignores_other_artifact_kinds_in_its_budget() -> None:
    entity_views = tuple(_entity_view(index) for index in range(1001))
    authority = BudgetedDerivations((*entity_views, manifest()))

    result = await ContextualEvidenceSearch(authority, Vectors(), PROFILE).search(
        query(), context=CONTEXT
    )

    assert [candidate.payload["chunk_id"] for candidate, _ in result] == ["chunk-2"]
    assert authority.kinds == ["contextual_chunk", "parent_description"]


class FailingContextualLane:
    """A contextual lane whose authority refuses the request."""

    async def search(self, query, *, context):
        del query, context
        raise HarborConflictError("derived artifact enumeration exceeds bounded limit")


def _runtime(contextual) -> RuntimeRetrievalService:
    return RuntimeRetrievalService(
        resources=replace(
            resources(vectors=TopologyVectors(), active_versions=ActiveVersions()),
            topology_repository=CanonicalTopology(),
            contextual_search=contextual,
        ),
        policy=policy(),
    )


async def test_contextual_failure_keeps_the_graph_candidates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, "harborrag.engine.retrieval.service.topology"):
        report = await _runtime(FailingContextualLane()).retrieve(
            "Alpha", tenant_id="tenant-1", options=LOCAL
        )

    assert [item.id for item in report.results] == ["chunk-1", "chunk-2"]
    assert "build-2" in report.results[1].metadata["topology_build_ids"]
    assert "topology_unavailable" not in report.evidence.coverage_gaps
    warning = next(entry for entry in caplog.records if "Contextual" in entry.getMessage())
    assert warning.error_code == "HarborConflictError"
    assert warning.tenant_id == "tenant-1"
    assert warning.request_id


async def test_graph_candidates_match_a_run_without_a_contextual_lane() -> None:
    without = await _runtime(None).retrieve("Alpha", tenant_id="tenant-1", options=LOCAL)
    failing = await _runtime(FailingContextualLane()).retrieve(
        "Alpha", tenant_id="tenant-1", options=LOCAL
    )

    assert [item.id for item in failing.results] == [item.id for item in without.results]
