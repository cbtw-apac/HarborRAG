from __future__ import annotations

from typing import Any

from harborrag_core.contracts.chunking import TextRefiner
from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.testing.chunking import CharacterCounter, CharacterRefiner
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
    ChunkingRequest,
    ChunkingService,
    ChunkStrategy,
    build_chunking_service,
)

# Everything sibling test modules import from here, including the two re-exported fakes.
__all__ = [
    "CharacterCounter",
    "CharacterRefiner",
    "make_config",
    "make_document",
    "make_profile",
    "make_request",
    "make_service",
]


def make_document(
    elements: list[DocumentElement],
    *,
    source: str = "local_file",
    content_type: str = "page",
    record_id: str | None = None,
    extra: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
) -> Document:
    return Document(
        id="doc-1",
        title="HarborRAG",
        content=elements,
        content_type=content_type,
        provenance=DocumentProvenance(
            source=source,
            record_id=record_id,
            extra=extra or {},
        ),
        raw=raw,
    )


def make_request(
    document: Document,
    *,
    profile_name: str | None = None,
    document_version_id: str = "document-version:1",
) -> ChunkingRequest:
    source = document.provenance.source.lower()
    connector_type = (
        "confluence" if "confluence" in source else "jira" if "jira" in source else "local"
    )
    return ChunkingRequest(
        tenant_id="tenant-1",
        document_version_id=document_version_id,
        connector_type=connector_type,
        document=document,
        profile_name=profile_name,
    )


def make_profile(
    *,
    name: str = "canonical",
    strategy: str = "canonical",
    minimum: int = 2,
    target: int = 20,
    maximum: int = 25,
    overlap: int = 0,
) -> ChunkingProfile:
    return ChunkingProfile(
        name=name,
        strategy=strategy,
        limits=ChunkingLimits(minimum, target, maximum, overlap),
    )


def make_config(
    profile: ChunkingProfile,
    *,
    configuration_version: str = "1",
    create_route_chunks: bool = False,
) -> ChunkingConfig:
    return ChunkingConfig(
        configuration_version=configuration_version,
        default_profile=profile.name,
        create_route_chunks=create_route_chunks,
        profiles={profile.name: profile},
        source_profiles={},
    )


def make_service(
    profile: ChunkingProfile,
    *,
    configuration_version: str = "1",
    refiner: TextRefiner | None = None,
    create_route_chunks: bool = False,
    additional_strategies: tuple[ChunkStrategy, ...] = (),
) -> ChunkingService:
    selected_refiner = CharacterRefiner() if refiner is None else refiner
    return build_chunking_service(
        config=make_config(
            profile,
            configuration_version=configuration_version,
            create_route_chunks=create_route_chunks,
        ),
        token_counter=CharacterCounter(),
        refiner=selected_refiner,
        additional_strategies=additional_strategies,
    )
