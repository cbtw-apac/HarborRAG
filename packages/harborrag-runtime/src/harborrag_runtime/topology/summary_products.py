"""Reuse accepted cards for the existing independently retriable graph/vector products."""

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.derived import ParentDescription
from harborrag_core.topology.extraction import digest

from .budgeted_extractor import EnrichmentDeferredError


async def summary_parents(
    control: IngestionControlPlaneDatabase,
    tenant_id: str,
    build: DocumentTopologyBuild,
) -> tuple[ParentDescription, ...]:
    records = await control.summaries.document_bindings(
        tenant_id, build.document_id, build.document_version_id
    )
    parents = []
    for node, binding in records:
        chunks = binding.manifest.input_chunk_ids
        if not chunks:
            continue
        document = binding.manifest.kind == "DocumentVersion"
        section = node.entity_type.value == "section"
        parents.append(
            ParentDescription(
                parent_key=build.document_id
                if document
                else digest([build.document_id, node.entity_type.value, node.logical_id]),
                level="document" if document else "section" if section else "structure",
                section_path=() if document else node.section_path,
                structure_id=None if document else node.logical_id,
                description=binding.card.description,
                input_chunk_ids=chunks,
                cited_chunk_ids=chunks,
                input_digest=binding.manifest.input_digest,
            )
        )
    documents = [parent for parent in parents if parent.level == "document"]
    if len(documents) != 1 or set(documents[0].input_chunk_ids) != set(build.chunk_ids):
        raise EnrichmentDeferredError("summary_projection_pending")
    return tuple(parents)
