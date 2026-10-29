from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from hashlib import sha256

from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.connections import ResolvedConnection
from harborrag_runtime.ingestion.profiles import build_processing_profile
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission, SourceSubmission
from harborrag_runtime.serialization import to_json_value
from harborrag_runtime.source_query import ProcessingProfileInput, SourceQuery


@dataclass(frozen=True, slots=True)
class SourceSubmissionDefaults:
    """Execution policy supplied by the selected provider's composition."""

    batch_size: int = 200
    document_concurrency: int = 8


def prepare_source_submission(
    settings: RuntimeSettings,
    submission: SourceSubmission,
    *,
    connection: ResolvedConnection,
    defaults: SourceSubmissionDefaults = SourceSubmissionDefaults(),
) -> PreparedSourceSubmission:
    """Turn a resolved connection and a submission into a provider-neutral run input.

    Args:
        settings: Runtime settings that pick the processing profile.
        submission: What the caller asked to ingest.
        connection: The connection snapshot the run will use.
        defaults: Batch and concurrency policy of the execution provider.

    Returns:
        The deterministic input every activity of the run receives.

    Raises:
        ConnectorConfigurationError: A local query names a filter the connector ignores.
    """
    if connection.provider == "local":
        _require_known_local_filters(submission.query)
    source_key = connection.snapshot.source_key
    query, config_filter_keys = _effective_query(connection, submission.query)
    source_scope_id = submission.source_scope_id or _source_scope_id(
        tenant_id=submission.tenant_id,
        connector_name=source_key,
        connector_type=connection.provider,
        connection_id=source_key,
        query=query,
        excluded_filters=config_filter_keys,
    )
    processing = build_processing_profile(settings)
    batch_size = submission.batch_size if submission.batch_size is not None else defaults.batch_size
    document_concurrency = (
        submission.document_concurrency
        if submission.document_concurrency is not None
        else defaults.document_concurrency
    )
    return PreparedSourceSubmission(
        task_id=submission.task_id,
        tenant_id=submission.tenant_id,
        connector_name=source_key,
        connector_type=connection.provider,
        connection_id=source_key,
        source_scope_id=source_scope_id,
        configuration_fingerprint=connection.fingerprint,
        processing=ProcessingProfileInput(
            parser_profile=processing.parser_profile,
            normalizer_version=processing.normalizer_version,
            chunk_strategy=processing.chunk_strategy,
            dense_encoder_profile=processing.dense_encoder_profile,
            sparse_encoder_profile=processing.sparse_encoder_profile,
            graph_projection_version=processing.graph_projection_version,
            vector_projection_schema=processing.vector_projection_schema,
        ),
        query=query,
        force_reprocess=submission.force_reprocess,
        discovery_page_size=submission.discovery_page_size,
        discovery_concurrency=submission.discovery_concurrency,
        document_concurrency=document_concurrency,
        missing_threshold=submission.missing_threshold,
        batch_size=batch_size,
        continue_after_batches=submission.continue_after_batches,
    )


def _require_known_local_filters(query: SourceQuery) -> None:
    """Reject filter keys the local connector would silently ignore."""

    from harborrag_adapters.connectors.local.filters import LOCAL_QUERY_FILTER_KEYS

    filters = json.loads(query.filters_json)
    if not isinstance(filters, dict):
        return
    unknown = sorted(str(key) for key in filters if key not in LOCAL_QUERY_FILTER_KEYS)
    if unknown:
        raise ConnectorConfigurationError(
            f"Unknown filter keys for a local connector: {', '.join(unknown)}. "
            f"Allowed keys: {', '.join(sorted(LOCAL_QUERY_FILTER_KEYS))}"
        )


def _source_scope_id(  # noqa: PLR0913 - the scope's identity fields
    *,
    tenant_id: str,
    connector_name: str,
    connector_type: str,
    connection_id: str,
    query: SourceQuery,
    excluded_filters: frozenset[str] = frozenset(),
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "connector_name": connector_name,
        "connector_type": connector_type,
        "connection_id": connection_id,
        "query": _scope_query(query, excluded_filters),
    }
    return f"scope-{_digest(payload)[:32]}"


def _effective_query(
    connection: ResolvedConnection, query: SourceQuery
) -> tuple[SourceQuery, frozenset[str]]:
    """Apply the connection's feature ceilings and config filters to the run query.

    Returns:
        The effective query and the filter keys that came from the connection's
        config; those stay out of the scope identity so editing them keeps the scope.
    """
    filters = json.loads(query.filters_json)
    from_config = {
        key: value for key, value in connection.query_filters().items() if key not in filters
    }
    effective = replace(
        query,
        include_attachments=query.include_attachments and connection.include_attachments(),
        filters_json=json.dumps(
            {**from_config, **filters}, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        if from_config
        else query.filters_json,
    )
    return effective, frozenset(from_config)


def _scope_query(
    query: SourceQuery, excluded_filters: frozenset[str] = frozenset()
) -> dict[str, object]:
    """Keep execution controls and connection config out of the stable source membership scope."""

    values = asdict(query)
    values.pop("include_attachments", None)
    filters = json.loads(query.filters_json)
    if isinstance(filters, dict):
        for key in ("include_comments", "build_graph", *excluded_filters):
            filters.pop(key, None)
        values["filters_json"] = json.dumps(
            filters,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return values


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        to_json_value(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
