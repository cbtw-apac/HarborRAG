from __future__ import annotations

from enum import StrEnum


class IngestionStage(StrEnum):
    DISCOVERY = "discovery"
    FETCH = "fetch"
    PARSE_NORMALIZE = "parse_normalize"
    CONTENT_SYNC = "content_sync"
    CANONICAL_PERSIST = "canonical_persist"
    CHUNK = "chunk"
    ENCODE = "encode"
    RELATION_BUILD = "relation_build"
    PROJECTION_BUILD = "projection_build"
    QDRANT_WRITE = "qdrant_write"
    FALKORDB_WRITE = "falkordb_write"
    VERIFICATION = "verification"
    PUBLICATION = "publication"
    FAILURE_CAPTURE = "failure_capture"
    FINALIZATION = "finalization"
    CANCELLATION = "cancellation"
    CONTROL = "control"
    CLEANUP = "cleanup"
    RELATION_REPAIR = "relation_repair"
    REINDEX = "reindex"


class DocumentMetricOutcome(StrEnum):
    DISCOVERED = "discovered"
    ADMITTED = "admitted"
    SKIPPED = "skipped"
    ACTIVATED = "activated"
    # Publication of the version that was already active (for example a
    # force-reprocess of unchanged content): nothing new was activated.
    REPUBLISHED = "republished"
    FAILED = "failed"
    REPLAYED = "replayed"


# ``document_kind`` label on stage metrics: what the stage is working on, in
# the same words as the document outcome, so dashboards filter both with one
# variable. A stage ends before the document's outcome is final, so this is the
# admission decision mapped forward, not the outcome itself.
DOCUMENT_KIND_NONE = "none"
DOCUMENT_KIND_OTHER = "other"
DOCUMENT_KINDS = ("activated", "republished", "skipped", DOCUMENT_KIND_OTHER)
_DECISION_KINDS = {
    "NEW": "activated",
    "UPDATED": "activated",
    "METADATA_CHANGED": "activated",
    "METADATA_ONLY": "activated",
    "FORCE_REPROCESS": "republished",
    "UNCHANGED": "skipped",
}


def document_kind(decision: str | None) -> str:
    """Map an admission decision to the ``document_kind`` stage label."""

    if decision is None:
        return DOCUMENT_KIND_NONE
    return _DECISION_KINDS.get(str(decision), DOCUMENT_KIND_OTHER)


class ArtifactMetricKind(StrEnum):
    RAW = "raw"
    CANONICAL = "canonical"


class ChunkMetricKind(StrEnum):
    ROUTE = "route"
    EVIDENCE = "evidence"
    TABLE = "table"
    REJECTED = "rejected"
