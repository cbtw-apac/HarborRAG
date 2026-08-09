from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG

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
DOCUMENT_STAGES = tuple(
    (stage.name, stage.activity, stage.task_queue) for stage in DOCUMENT_STAGE_CATALOG
)
