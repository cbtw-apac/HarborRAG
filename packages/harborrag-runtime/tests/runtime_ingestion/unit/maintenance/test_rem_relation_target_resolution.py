"""Target lookups of one repair stay bounded, and the first failure ends the others."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest

from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion.maintenance.relation_repair import GraphRelationRepairService

pytestmark = pytest.mark.unit

_SAME_CONNECTOR = "jira://OPS/OPS-1"
_OTHER_CONNECTOR = "confluence://ENG/77"
_DOCUMENT = SimpleNamespace(
    provenance=SimpleNamespace(extra={"connector_type": "jira", "connection_id": "jira.example"})
)


class _CatalogTimeoutError(RuntimeError):
    """A control database that stopped answering."""


def _service(document_versions: object, *, max_concurrency: int) -> GraphRelationRepairService:
    return GraphRelationRepairService(
        control=cast(Any, SimpleNamespace(document_versions=document_versions)),
        canonical_artifacts=cast(Any, SimpleNamespace()),
        chunk_reader=cast(Any, SimpleNamespace()),
        graph_store=cast(Any, SimpleNamespace()),
        max_concurrency=max_concurrency,
    )


async def _resolve(service: GraphRelationRepairService) -> dict[str, object]:
    return dict(
        await service._resolve_targets(
            cast(Any, _DOCUMENT),
            (_SAME_CONNECTOR, _OTHER_CONNECTOR),
            context=StorageOperationContext.system("tenant-1"),
        )
    )


@pytest.mark.asyncio
async def test_a_failing_target_lookup_cancels_the_other_and_raises_as_itself() -> None:
    cancelled: list[str] = []
    never = asyncio.Event()

    async def resolve_active_sources(**kwargs: object) -> dict[str, object]:
        del kwargs
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.append("same-connector")
            raise
        return {}

    async def resolve_unambiguous_active_sources(**kwargs: object) -> dict[str, object]:
        del kwargs
        await asyncio.sleep(0)
        raise _CatalogTimeoutError("catalog timed out") from TimeoutError()

    service = _service(
        SimpleNamespace(
            resolve_active_sources=resolve_active_sources,
            resolve_unambiguous_active_sources=resolve_unambiguous_active_sources,
        ),
        max_concurrency=2,
    )

    with pytest.raises(_CatalogTimeoutError) as caught:
        async with asyncio.timeout(5):
            await _resolve(service)

    assert isinstance(caught.value.__cause__, TimeoutError)
    assert cancelled == ["same-connector"]


@pytest.mark.asyncio
async def test_target_lookups_stay_within_the_concurrency_bound() -> None:
    in_flight = 0
    peak = 0

    async def lookup(source_item_ids: Sequence[str]) -> dict[str, object]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return {source_item_id: f"target:{source_item_id}" for source_item_id in source_item_ids}

    async def resolve_active_sources(
        *, source_item_ids: Sequence[str], **kwargs: object
    ) -> dict[str, object]:
        del kwargs
        return await lookup(source_item_ids)

    async def resolve_unambiguous_active_sources(
        *, source_item_ids: Sequence[str], **kwargs: object
    ) -> dict[str, object]:
        del kwargs
        return await lookup(source_item_ids)

    service = _service(
        SimpleNamespace(
            resolve_active_sources=resolve_active_sources,
            resolve_unambiguous_active_sources=resolve_unambiguous_active_sources,
        ),
        max_concurrency=1,
    )

    resolved = await _resolve(service)

    assert resolved == {
        _SAME_CONNECTOR: f"target:{_SAME_CONNECTOR}",
        _OTHER_CONNECTOR: f"target:{_OTHER_CONNECTOR}",
    }
    assert peak == 1
