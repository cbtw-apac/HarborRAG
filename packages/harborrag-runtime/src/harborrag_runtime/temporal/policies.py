from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

DISCOVERY_QUEUE = "harborrag-discovery"
IO_QUEUE = "harborrag-io"
PARSER_QUEUE = "harborrag-parser"
TRANSFORM_QUEUE = "harborrag-transform"
MODEL_QUEUE = "harborrag-model"
INDEX_QUEUE = "harborrag-index"

DISCOVERY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=8,
)
DOCUMENT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
)
DOCUMENT_STAGES = (
    ("SyncContentUnits", "harborrag.sync_content_units", TRANSFORM_QUEUE),
    ("PersistCanonical", "harborrag.persist_canonical", IO_QUEUE),
    ("ChunkAndValidate", "harborrag.chunk_and_validate", TRANSFORM_QUEUE),
    ("EncodeChunks", "harborrag.encode_chunks", MODEL_QUEUE),
    ("BuildRelations", "harborrag.build_relations", TRANSFORM_QUEUE),
    ("BuildProjections", "harborrag.build_projections", TRANSFORM_QUEUE),
    ("WriteVectorProjection", "harborrag.write_vector_projection", INDEX_QUEUE),
    ("WriteGraphProjection", "harborrag.write_graph_projection", INDEX_QUEUE),
    ("VerifyProjections", "harborrag.verify_projections", INDEX_QUEUE),
)
