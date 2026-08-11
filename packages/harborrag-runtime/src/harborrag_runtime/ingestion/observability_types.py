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
    CLEANUP = "cleanup"
    RELATION_REPAIR = "relation_repair"
    REINDEX = "reindex"


class DocumentMetricOutcome(StrEnum):
    DISCOVERED = "discovered"
    ADMITTED = "admitted"
    SKIPPED = "skipped"
    ACTIVATED = "activated"
    FAILED = "failed"
    REPLAYED = "replayed"


class ArtifactMetricKind(StrEnum):
    RAW = "raw"
    CANONICAL = "canonical"


class ChunkMetricKind(StrEnum):
    ROUTE = "route"
    EVIDENCE = "evidence"
    TABLE = "table"
    REJECTED = "rejected"
