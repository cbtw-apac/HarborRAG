"""Independent semantic topology lifecycle and extraction contracts."""

from .extraction import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionIncompleteError,
    ExtractionOutput,
    ExtractionProfile,
    digest,
)
from .ontology import OntologyRegistry, RelationDefinition, builtin_ontology
from .records import (
    CanonicalAssertion,
    CanonicalMention,
    ChunkExtractionCheckpoint,
    DocumentTopologyBuild,
    TopologyJob,
    TopologyPolicy,
)
from .resolution import ResolutionDecision, ResolutionRequest
from .revisions import CONSERVATIVE_RESOLUTION_REVISION, projection_revision_for_schema
from .windowing import (
    ExtractionWindow,
    ExtractionWindowBuilder,
    extraction_windows,
    merge_window_outputs,
)

__all__ = [
    "CanonicalAssertion",
    "CanonicalMention",
    "ChunkExtractionCheckpoint",
    "ChunkExtractionInput",
    "DocumentTopologyBuild",
    "EvidenceSpan",
    "ExtractedAssertion",
    "ExtractedEntity",
    "ExtractionOutput",
    "ExtractionIncompleteError",
    "ExtractionProfile",
    "TopologyJob",
    "TopologyPolicy",
    "CONSERVATIVE_RESOLUTION_REVISION",
    "projection_revision_for_schema",
    "ResolutionDecision",
    "ResolutionRequest",
    "digest",
    "OntologyRegistry",
    "RelationDefinition",
    "builtin_ontology",
    "ExtractionWindow",
    "ExtractionWindowBuilder",
    "extraction_windows",
    "merge_window_outputs",
]
