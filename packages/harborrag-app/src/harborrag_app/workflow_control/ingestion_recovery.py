"""Rebuild safe workflow inputs from durable ingestion task requests."""

from __future__ import annotations

import json
from collections.abc import Mapping

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.ingestion import IngestionTask
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    RetryFailuresInput,
    SourceIngestionInput,
    SourceQuery,
)

DEFAULT_TENANT_ID = "DEFAULT"


def source_from_task(task: IngestionTask) -> SourceIngestionInput:
    """Rebuild the safe workflow input for a retried durable submission."""

    request = task.request
    processing = _mapping(request.get("processing"), "processing")
    query = _mapping(request.get("query"), "query")
    filters = query.get("filters")
    limit = query.get("limit")
    return SourceIngestionInput(
        task_id=task.task_id,
        tenant_id=str(request.get("tenant_id") or DEFAULT_TENANT_ID),
        connector_name=str(request["connector_name"]),
        connector_type=str(request["connector_type"]),
        connection_id=str(request["connection_id"]),
        source_scope_id=str(request["source_scope_id"]),
        configuration_fingerprint=str(request["configuration_fingerprint"]),
        processing=_processing_profile(processing),
        query=SourceQuery(
            path=_optional_text(query.get("path")),
            pattern=_optional_text(query.get("pattern")),
            recursive=query.get("recursive") is not False,
            updated_after=_optional_text(query.get("updated_after")),
            limit=limit if isinstance(limit, int) else None,
            include_attachments=query.get("include_attachments") is not False,
            filters_json=json.dumps(
                filters if isinstance(filters, Mapping) else {},
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
        force_reprocess=request.get("force_reprocess") is True,
    )


def retry_from_task(task: IngestionTask) -> RetryFailuresInput:
    """Rebuild a safe retry workflow input from its durable task request."""

    request = task.request
    original_task_id = request.get("retry_of")
    document_ids = request.get("document_ids")
    if not isinstance(original_task_id, str) or not original_task_id.strip():
        raise HarborValidationError("stored retry original task is invalid")
    if not isinstance(document_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in document_ids
    ):
        raise HarborValidationError("stored retry document identifiers are invalid")
    return RetryFailuresInput(
        retry_task_id=task.task_id,
        original_task_id=original_task_id,
        tenant_id=str(request.get("tenant_id") or DEFAULT_TENANT_ID),
        document_ids=tuple(document_ids),
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise HarborValidationError(f"stored ingestion {label} is invalid")
    return dict(value)


def _processing_profile(stored: Mapping[str, object]) -> ProcessingProfileInput:
    """Rebuild the processing profile, naming each field the durable record must supply.

    Splatting the stored mapping would let a schema drift reach the constructor as a
    runtime TypeError; listing the fields turns that into a validation error here.
    """

    def field(name: str) -> str:
        value = stored.get(name)
        if not isinstance(value, str) or not value.strip():
            raise HarborValidationError(f"stored processing profile {name!r} is invalid")
        return value

    schema = stored.get("vector_projection_schema")
    return ProcessingProfileInput(
        parser_profile=field("parser_profile"),
        normalizer_version=field("normalizer_version"),
        chunk_strategy=field("chunk_strategy"),
        dense_encoder_profile=field("dense_encoder_profile"),
        sparse_encoder_profile=field("sparse_encoder_profile"),
        graph_projection_version=field("graph_projection_version"),
        **({"vector_projection_schema": schema} if isinstance(schema, str) else {}),
    )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = ["DEFAULT_TENANT_ID", "retry_from_task", "source_from_task"]
