from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.contracts.chunking import (
    JsonStructureSplitRequest,
    JsonStructureSplitter,
    SourceSpan,
    SplitBoundaryKind,
    StructureSplitRequest,
    StructureSplitter,
    TextRefinementRequest,
    TextRefiner,
    TextSplit,
)
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.normalized_document import Document
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
    ChunkingRequest,
    ChunkingService,
    build_default_chunking_service,
)


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


class CharacterRefiner:
    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        if not request.content:
            return ()
        results = []
        start = 0
        while start < len(request.content):
            end = min(start + request.maximum_tokens, len(request.content))
            base = request.source_span
            base_offset = base.start_offset if base and base.start_offset is not None else 0
            results.append(
                TextSplit(
                    content=request.content[start:end],
                    token_count=end - start,
                    source_span=SourceSpan(
                        start_offset=base_offset + start,
                        end_offset=base_offset + end,
                        element_ids=base.element_ids if base else (),
                    ),
                    boundary_kind=SplitBoundaryKind.FORCED,
                    structural_path=request.structural_path,
                    forced_split=True,
                )
            )
            start = end
        return tuple(results)


class RootJsonSplitter:
    def split(self, request: JsonStructureSplitRequest) -> tuple[TextSplit, ...]:
        value: Mapping[str, Any] | Sequence[Any] = request.value
        return (
            TextSplit(
                content=str(value),
                token_count=len(str(value)),
                source_span=request.source_span,
                boundary_kind=SplitBoundaryKind.JSON_PATH,
                structural_path=("root",),
            ),
        )


class StaticStructureSplitter:
    def split(self, request: StructureSplitRequest) -> tuple[TextSplit, ...]:
        return (
            TextSplit(
                content="Recovered section",
                token_count=len("Recovered section"),
                source_span=request.source_span,
                boundary_kind=SplitBoundaryKind.SECTION,
                structural_path=("Guide", "Setup"),
            ),
        )


class EchoStructureSplitter:
    def __init__(self) -> None:
        self.contents: list[str] = []

    def split(self, request: StructureSplitRequest) -> tuple[TextSplit, ...]:
        self.contents.append(request.content)
        return (
            TextSplit(
                content=request.content,
                token_count=len(request.content),
                source_span=request.source_span,
                boundary_kind=SplitBoundaryKind.SECTION,
            ),
        )


class EmptyStructureSplitter:
    def split(self, request: StructureSplitRequest) -> tuple[TextSplit, ...]:
        del request
        return ()


class EmptyJsonSplitter:
    def split(self, request: JsonStructureSplitRequest) -> tuple[TextSplit, ...]:
        del request
        return ()


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
    artifact_revision_id: str = "revision-1",
) -> ChunkingRequest:
    return ChunkingRequest(
        tenant_id="tenant-1",
        artifact_id=document.id,
        artifact_revision_id=artifact_revision_id,
        document=document,
        profile_name=profile_name,
    )


def make_profile(
    *,
    name: str = "document",
    strategy: str = "document",
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
) -> ChunkingConfig:
    return ChunkingConfig(
        configuration_version=configuration_version,
        default_profile=profile.name,
        profiles={profile.name: profile},
        routes=(),
    )


def make_service(
    profile: ChunkingProfile,
    *,
    configuration_version: str = "1",
    json_splitter: JsonStructureSplitter | None = None,
    markdown_splitter: StructureSplitter | None = None,
    html_splitter: StructureSplitter | None = None,
    refiner: TextRefiner | None = None,
) -> ChunkingService:
    selected_refiner = CharacterRefiner() if refiner is None else refiner
    return build_default_chunking_service(
        config=make_config(profile, configuration_version=configuration_version),
        token_counter=CharacterCounter(),
        refiner=selected_refiner,
        json_splitter=json_splitter,
        markdown_splitter=markdown_splitter,
        html_splitter=html_splitter,
    )
