"""Topology inspection joins generated graph views with exact canonical evidence."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from topology_service_support import Harness

from harborrag_core.base import utc_now
from harborrag_core.topology import ExtractionOutput
from harborrag_core.topology.derived import ChunkEnrichment
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
from harborrag_runtime.topology import operations
from harborrag_runtime.topology.inspection import TopologyInspectionBuilder


async def _permit_reader(harness: Harness, document_id: str) -> None:
    now = utc_now()
    for kind, resource in (("source", "scope"), ("document", document_id)):
        await harness.control.topology.set_permissions(
            ResolvedPermissionSnapshot(
                tenant_id="DEFAULT",
                resource_kind=kind,
                resource_id=resource,
                revision="acl-reader-1",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
                known=True,
                processing_allowed=True,
                allowed_principal_ids=("reader",),
            )
        )


@pytest.mark.asyncio
async def test_inspection_exposes_llm_provenance_descriptions_and_exact_evidence(
    tmp_path, monkeypatch
):
    async with Harness(tmp_path) as harness:
        async def permit_reader(document_id: str) -> None:
            await _permit_reader(harness, document_id)

        harness.permit = permit_reader
        content = "Harbor supersedes Dock."
        await harness.publish(content=content)
        harbor = {"start": 0, "end": 6, "quote": "Harbor"}
        dock = {"start": 18, "end": 22, "quote": "Dock"}
        statement = {"start": 0, "end": 22, "quote": "Harbor supersedes Dock"}
        harness.model.output = ExtractionOutput.model_validate(
            {
                "title": "Harbor migration",
                "description": "Harbor replaces Dock.",
                "retrieval_context": "Migration relationship between Harbor and Dock.",
                "title_evidence": [harbor],
                "description_evidence": [statement],
                "retrieval_context_evidence": [statement],
                "entities": [
                    {
                        "local_id": "harbor",
                        "name": "Harbor",
                        "entity_type": "service",
                        "span": harbor,
                    },
                    {
                        "local_id": "dock",
                        "name": "Dock",
                        "entity_type": "service",
                        "span": dock,
                    },
                ],
                "assertions": [
                    {
                        "local_id": "migration",
                        "subject_id": "harbor",
                        "object_id": "dock",
                        "predicate": "supersedes",
                        "span": statement,
                        "statement_text": "Harbor supersedes Dock",
                    }
                ],
            }
        )
        result = await harness.service.run_once("DEFAULT")
        assert result.state == "accepted"

        @asynccontextmanager
        async def authority(_settings):
            yield harness.control

        monkeypatch.setattr(operations, "connect_topology_authority", authority)
        view = await operations.inspect_build(
            None, "DEFAULT", result.build_id, principal_id="reader", limit=10
        )
        assert view["generation"]["method"] == "llm_structured_extraction"
        assert view["generation"]["model"] == "test"
        assert view["generation"]["checkpoint_count"] == 1
        assert view["representations"][0]["generated"]["description"] == (
            "Harbor replaces Dock."
        )
        assert view["mentions"][0]["evidence"]["quote"] in {"Harbor", "Dock"}
        assert view["assertions"][0]["statement_text"] == "Harbor supersedes Dock"
        assert view["assertions"][0]["evidence"]["quote"] == "Harbor supersedes Dock"

        with pytest.raises(ValueError, match="unavailable"):
            await operations.inspect_build(
                None, "DEFAULT", result.build_id, principal_id="stranger", limit=10
            )


def test_inspection_omits_empty_retired_retrieval_context() -> None:
    result = TopologyInspectionBuilder._representation(
        ChunkEnrichment(chunk_id="chunk-1", title="A title", description="A description"),
        None,
    )

    assert result["generated"] == {
        "title": "A title",
        "description": "A description",
    }
