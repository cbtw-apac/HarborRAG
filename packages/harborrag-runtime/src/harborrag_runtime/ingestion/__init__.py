"""Public ingestion contracts without eager production-runtime composition."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .composition import IngestionRuntime, build_ingestion_runtime
    from .document.dependencies import DocumentReleaseDependencies
    from .document.models import DocumentReleaseOutcome, DocumentReleaseRequest
    from .document.normalizers import (
        SourceDocumentNormalizerBuilder,
        SourceNormalizerRegistration,
        default_source_document_normalizer_builder,
    )
    from .document.service import DocumentReleaseService
    from .maintenance.cleanup import ProjectionCleanupBatch, ProjectionCleanupService
    from .maintenance.reindex import (
        DocumentReindexService,
        ReindexRequest,
        source_identity_from_canonical,
    )
    from .maintenance.reindex_plan import ReindexPlan, processing_profile_from_canonical
    from .maintenance.relation_repair import GraphRelationRepairService, RelationRepairResult
    from .runtime_builder import IngestionRuntimeBuilder
    from .source.models import SourceIngestionOutcome, SourceIngestionRequest
    from .source.plan import SourcePlanRepository
    from .source.service import SourceIngestionService

__all__ = [
    "DocumentReleaseDependencies",
    "DocumentReleaseOutcome",
    "DocumentReleaseRequest",
    "DocumentReleaseService",
    "DocumentReindexService",
    "GraphRelationRepairService",
    "IngestionRuntime",
    "IngestionRuntimeBuilder",
    "ProjectionCleanupBatch",
    "ProjectionCleanupService",
    "RelationRepairResult",
    "ReindexRequest",
    "ReindexPlan",
    "SourceIngestionOutcome",
    "SourceIngestionRequest",
    "SourceIngestionService",
    "SourceDocumentNormalizerBuilder",
    "SourceNormalizerRegistration",
    "SourcePlanRepository",
    "build_ingestion_runtime",
    "default_source_document_normalizer_builder",
    "source_identity_from_canonical",
    "processing_profile_from_canonical",
]

_EXPORT_MODULES = {
    "DocumentReleaseDependencies": "harborrag_runtime.ingestion.document.dependencies",
    "DocumentReleaseOutcome": "harborrag_runtime.ingestion.document.models",
    "DocumentReleaseRequest": "harborrag_runtime.ingestion.document.models",
    "DocumentReleaseService": "harborrag_runtime.ingestion.document.service",
    "DocumentReindexService": "harborrag_runtime.ingestion.maintenance.reindex",
    "GraphRelationRepairService": "harborrag_runtime.ingestion.maintenance.relation_repair",
    "IngestionRuntime": "harborrag_runtime.ingestion.composition",
    "IngestionRuntimeBuilder": "harborrag_runtime.ingestion.runtime_builder",
    "ProjectionCleanupBatch": "harborrag_runtime.ingestion.maintenance.cleanup",
    "ProjectionCleanupService": "harborrag_runtime.ingestion.maintenance.cleanup",
    "RelationRepairResult": "harborrag_runtime.ingestion.maintenance.relation_repair",
    "ReindexRequest": "harborrag_runtime.ingestion.maintenance.reindex",
    "ReindexPlan": "harborrag_runtime.ingestion.maintenance.reindex_plan",
    "SourceDocumentNormalizerBuilder": "harborrag_runtime.ingestion.document.normalizers",
    "SourceIngestionOutcome": "harborrag_runtime.ingestion.source.models",
    "SourceIngestionRequest": "harborrag_runtime.ingestion.source.models",
    "SourceIngestionService": "harborrag_runtime.ingestion.source.service",
    "SourceNormalizerRegistration": "harborrag_runtime.ingestion.document.normalizers",
    "SourcePlanRepository": "harborrag_runtime.ingestion.source.plan",
    "build_ingestion_runtime": "harborrag_runtime.ingestion.composition",
    "default_source_document_normalizer_builder": (
        "harborrag_runtime.ingestion.document.normalizers"
    ),
    "processing_profile_from_canonical": (
        "harborrag_runtime.ingestion.maintenance.reindex_plan"
    ),
    "source_identity_from_canonical": "harborrag_runtime.ingestion.maintenance.reindex",
}


def __getattr__(name: str) -> Any:
    """Import an ingestion service only when a caller requests that service."""

    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy public exports in interactive discovery."""

    return sorted(set(globals()) | set(__all__))
