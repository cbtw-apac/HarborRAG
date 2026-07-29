from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from harborrag_core.chunking import ChunkRecord, TableProjectionType
from harborrag_core.domain import TableArtifact


class TableShape(StrEnum):
    SMALL = "small"
    LONG = "long"
    WIDE = "wide"
    LARGE = "large"
    MATRIX = "matrix"
    TIME_SERIES = "time_series"


class TableChunkRole(StrEnum):
    ROUTE = "route"
    SCHEMA = "schema"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True)
class TableClassification:
    shape: TableShape
    confidence: float
    estimated_tokens: int
    key_column_indices: tuple[int, ...]
    key_column_confidences: Mapping[int, float]
    time_column_index: int | None = None
    time_column_confidence: float = 0.0
    row_label_confidence: float = 0.0
    column_label_confidence: float = 0.0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1 or self.estimated_tokens < 0:
            raise ValueError("table classification confidence/tokens are invalid")
        object.__setattr__(
            self,
            "key_column_confidences",
            MappingProxyType(dict(self.key_column_confidences)),
        )


@dataclass(frozen=True, slots=True)
class PlannedTableChunk:
    role: TableChunkRole
    projection_type: TableProjectionType
    row_start: int
    row_end: int
    selected_column_indices: tuple[int, ...]
    repeated_key_column_indices: tuple[int, ...] = ()
    repeated_header_row_count: int = 0


@dataclass(frozen=True, slots=True)
class TablePlan:
    classification: TableClassification
    chunks: tuple[PlannedTableChunk, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TableChunkingRequest:
    artifact: TableArtifact
    tenant_id: str
    connection_id: str
    source_scope: str
    page_title: str
    space: str
    permissions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = (
            self.tenant_id,
            self.connection_id,
            self.source_scope,
            self.page_title,
            self.space,
        )
        if any(not value.strip() for value in values):
            raise ValueError("table chunking request context must be non-empty")
        object.__setattr__(self, "permissions", MappingProxyType(dict(self.permissions)))


@dataclass(frozen=True, slots=True)
class TableQualityMetrics:
    boundary_score: float
    self_containment_score: float
    header_completeness_score: float
    provenance_score: float
    noise_score: float
    warnings: tuple[str, ...] = ()

    @property
    def score(self) -> float:
        return (
            self.boundary_score
            + self.self_containment_score
            + self.header_completeness_score
            + self.provenance_score
            + (1.0 - self.noise_score)
        ) / 5


@dataclass(frozen=True, slots=True)
class TableChunkingResult:
    artifact: TableArtifact
    classification: TableClassification
    chunks: tuple[ChunkRecord, ...]
    quality: Mapping[str, TableQualityMetrics]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "quality", MappingProxyType(dict(self.quality)))
