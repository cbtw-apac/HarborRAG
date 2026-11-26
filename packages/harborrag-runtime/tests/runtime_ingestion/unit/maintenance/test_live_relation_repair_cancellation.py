"""A failed repair stops the pass's other repairs, so a retried finalization starts clean."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from harborrag_runtime.ingestion.maintenance.relation_repair import GraphRelationRepairService

pytestmark = pytest.mark.unit

_BROKEN = "doc-broken"


class _GraphStoreTimeoutError(RuntimeError):
    """A graph store that stopped answering."""


def _planned(document_id: str) -> Any:
    processing = SimpleNamespace(graph_projection_version="graph-v2")
    return SimpleNamespace(document_id=document_id, request=SimpleNamespace(processing=processing))


@pytest.mark.asyncio
async def test_one_failing_repair_cancels_the_others_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repair = GraphRelationRepairService(
        control=cast(Any, SimpleNamespace()),
        canonical_artifacts=cast(Any, SimpleNamespace()),
        chunk_reader=cast(Any, SimpleNamespace()),
        graph_store=cast(Any, SimpleNamespace()),
        max_concurrency=3,
    )
    monkeypatch.setattr(repair._fingerprints, "processing_fingerprint", lambda **_: "fp")
    started: list[str] = []
    cancelled: list[str] = []
    in_flight = 0
    peak = 0
    never = asyncio.Event()

    async def repair_one(document_id: str, **kwargs: object) -> tuple[int, int, int]:
        nonlocal in_flight, peak
        del kwargs
        in_flight += 1
        peak = max(peak, in_flight)
        started.append(document_id)
        try:
            if document_id == _BROKEN:
                await asyncio.sleep(0)
                raise _GraphStoreTimeoutError("graph store timed out") from TimeoutError()
            await never.wait()
        except asyncio.CancelledError:
            cancelled.append(document_id)
            raise
        finally:
            in_flight -= 1
        return (1, 0, 0)

    monkeypatch.setattr(repair, "_repair_one", repair_one)
    planned = [_planned(name) for name in ("doc-0", "doc-1", _BROKEN, "doc-3", "doc-4")]

    with pytest.raises(_GraphStoreTimeoutError) as caught:
        async with asyncio.timeout(5):
            await repair.repair(planned, tenant_id="tenant-1")

    assert isinstance(caught.value.__cause__, TimeoutError)
    assert started[:3] == ["doc-0", "doc-1", _BROKEN]
    assert sorted(cancelled) == sorted(set(started) - {_BROKEN})
    assert "doc-4" not in started
    assert peak == 3
    assert in_flight == 0
