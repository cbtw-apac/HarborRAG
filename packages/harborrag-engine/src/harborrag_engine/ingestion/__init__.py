from harborrag_engine.ingestion.base import (
    BaseChunker,
    BaseDocumentNormalizer,
    BaseIngestionPipeline,
    IngestionRunSummary,
)

__all__ = [
    "BaseChunker",
    "BaseDocumentNormalizer",
    "BaseIngestionPipeline",
    "IngestionRunSummary",
]

try:
    # ingestion.mock depends on harborrag_adapters.models.embedding (needs
    # harborrag_core.ports.models) and harborrag_core.domain.{metadata,provenance},
    # none of which exist yet. Isolate the failure here so it doesn't take down
    # the base contracts re-exported by this package.
    from harborrag_engine.ingestion.mock import (
        MockChunker,
        MockDocumentNormalizer,
        MockIngestionPipeline,
    )
except ImportError:
    pass
else:  # pragma: no cover - unreachable until the core/adapters gaps above exist
    __all__ += ["MockChunker", "MockDocumentNormalizer", "MockIngestionPipeline"]
