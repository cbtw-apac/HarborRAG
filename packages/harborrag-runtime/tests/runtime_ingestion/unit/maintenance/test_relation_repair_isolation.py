"""The reverse pass repairs other runs' declarers, so their failures must not be this run's."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_runtime.ingestion.maintenance.relation_repair import GraphRelationRepairService

pytestmark = pytest.mark.unit


class _Unresolved:
    def __init__(self, *pages: tuple[str, ...]) -> None:
        self._pages = list(pages)

    async def resolvable_declaring_documents(self, **kwargs: object) -> tuple[str, ...]:
        del kwargs
        return self._pages.pop(0) if self._pages else ()


class _Graph:
    def __init__(self) -> None:
        self.prunes = 0

    async def prune_external_stubs(self, *, context: object) -> int:
        del context
        self.prunes += 1
        return 0


def _service(graph: _Graph, *pages: tuple[str, ...]) -> GraphRelationRepairService:
    """Build the service over stand-ins that answer only the calls a repair pass makes."""
    control: Any = SimpleNamespace(unresolved_relations=_Unresolved(*pages))
    unused: Any = SimpleNamespace()
    graph_store: Any = graph
    return GraphRelationRepairService(
        control=control,
        canonical_artifacts=unused,
        chunk_reader=unused,
        graph_store=graph_store,
    )


async def _repair_one(document_id: str, **kwargs: object) -> tuple[int, int, int]:
    del kwargs
    if document_id == "foreign-broken":
        raise ValueError("repaired graph projection failed verification")
    return (1, 2, 0)


def _planned(document_id: str) -> SimpleNamespace:
    processing = SimpleNamespace(graph_projection_version="graph-v2")
    return SimpleNamespace(document_id=document_id, request=SimpleNamespace(processing=processing))


def _event(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    """Return the one captured record of ``event``, with no dropped fields."""
    records = [record for record in caplog.records if record.__dict__.get("event") == event]
    assert len(records) == 1
    assert "dropped_fields" not in records[0].__dict__
    return records[0]


@pytest.mark.asyncio
async def test_a_failing_foreign_declarer_is_skipped_and_the_rest_still_repair(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing foreign declarer is skipped and the rest still repair.

    Its unresolved rows stay put, so a later run retries it, and this run is not failed.
    The pass also counts its failures once, so a backend outage that fails every declarer
    shows, and it prunes stubs exactly once, after the whole pass.
    """
    graph = _Graph()
    repair = _service(graph, ("foreign-a", "foreign-broken"), ("foreign-c",))
    monkeypatch.setattr(repair, "_repair_one", _repair_one)

    result = await repair.repair_resolvable(tenant_id="tenant-1", graph_projection_version="v2")

    assert result.repaired_documents == 2
    assert result.resolved_relations == 4
    declarer = _event(caplog, "ingestion.relation_repair_declarer_failed")
    assert declarer.levelno == logging.WARNING
    assert declarer.__dict__["document_id"] == "foreign-broken"
    assert declarer.__dict__["error_type"] == "ValueError"
    skipped = _event(caplog, "ingestion.relation_repair_declarers_skipped")
    assert skipped.levelno == logging.WARNING
    assert (skipped.__dict__["failed_count"], skipped.__dict__["visited_count"]) == (1, 3)
    assert graph.prunes == 1


@pytest.mark.asyncio
async def test_the_runs_own_forward_repair_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repair = _service(_Graph())
    monkeypatch.setattr(repair, "_repair_one", _repair_one)
    monkeypatch.setattr(repair._fingerprints, "processing_fingerprint", lambda **_: "fp")

    with pytest.raises(ValueError, match="failed verification"):
        await repair.repair((_planned("own-a"), _planned("foreign-broken")), tenant_id="tenant-1")


@pytest.mark.asyncio
async def test_a_forward_repair_page_does_not_scan_for_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalization repairs one plan page at a time; a prune scans the tenant's whole graph."""
    graph = _Graph()
    repair = _service(graph)
    monkeypatch.setattr(repair, "_repair_one", _repair_one)
    monkeypatch.setattr(repair._fingerprints, "processing_fingerprint", lambda **_: "fp")

    for page in (("a", "b"), ("c",)):
        await repair.repair(tuple(_planned(item) for item in page), tenant_id="tenant-1")

    assert graph.prunes == 0
