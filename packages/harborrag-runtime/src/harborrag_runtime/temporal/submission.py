from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from inspect import Parameter, signature

from harborrag_runtime.config import (
    ConnectorDefinition,
    connector_fingerprint,
    load_connector_catalog,
)
from harborrag_runtime.config.connectors.providers import (
    coerce_config_values,
    config_factory,
)
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.profiles import build_processing_profile
from harborrag_runtime.serialization import to_json_value

from .schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    SourceQuery,
)


@dataclass(frozen=True, slots=True)
class SourceSubmission:
    """Secret-free public request used to construct one source workflow input."""

    task_id: str
    tenant_id: str
    connector_name: str
    connection_id: str | None = None
    source_scope_id: str | None = None
    query: SourceQuery = SourceQuery()
    force_reprocess: bool = False
    discovery_page_size: int = 50
    discovery_concurrency: int = 4
    document_concurrency: int = 8
    missing_threshold: int = 2
    batch_size: int = 200
    continue_after_batches: int = 25


def build_source_input(
    settings: RuntimeSettings,
    submission: SourceSubmission,
    *,
    environment: Mapping[str, str] | None = None,
) -> SourceIngestionInput:
    """Resolve a configured connector into a deterministic workflow contract."""

    catalog = load_connector_catalog(settings.connector_config_path)
    definition = catalog.get(submission.connector_name)
    if not definition.enabled:
        raise ConnectorConfigurationError(
            f"Configured connector {submission.connector_name!r} is disabled"
        )
    source_environment = os.environ if environment is None else environment
    effective_connection_id = submission.connection_id or submission.connector_name
    configuration_fingerprint = connector_fingerprint(
        catalog_version=catalog.version,
        definition=definition,
        environment=source_environment,
    )
    query = _effective_query(
        definition,
        submission.query,
        environment=source_environment,
    )
    source_scope_id = submission.source_scope_id or _source_scope_id(
        tenant_id=submission.tenant_id,
        connector_name=submission.connector_name,
        connector_type=definition.provider,
        connection_id=effective_connection_id,
        query=query,
    )
    processing = build_processing_profile(settings)
    return SourceIngestionInput(
        task_id=submission.task_id,
        tenant_id=submission.tenant_id,
        connector_name=submission.connector_name,
        connector_type=definition.provider,
        connection_id=effective_connection_id,
        source_scope_id=source_scope_id,
        configuration_fingerprint=configuration_fingerprint,
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
        document_concurrency=submission.document_concurrency,
        missing_threshold=submission.missing_threshold,
        batch_size=submission.batch_size,
        continue_after_batches=submission.continue_after_batches,
    )


def _source_scope_id(
    *,
    tenant_id: str,
    connector_name: str,
    connector_type: str,
    connection_id: str,
    query: SourceQuery,
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "connector_name": connector_name,
        "connector_type": connector_type,
        "connection_id": connection_id,
        "query": _scope_query(query),
    }
    return f"scope-{_digest(payload)[:32]}"


def _effective_query(
    definition: ConnectorDefinition,
    query: SourceQuery,
    *,
    environment: Mapping[str, str],
) -> SourceQuery:
    """Apply connector-level feature ceilings to the durable query contract."""

    factory = config_factory(definition.provider)
    if factory is None:
        return query
    parameter = signature(factory).parameters.get("include_attachments")
    if parameter is None:
        return query
    settings = dict(definition.settings)
    for field_name, variable_name in definition.setting_environment.items():
        settings[field_name] = environment[variable_name]
    values = coerce_config_values(factory, settings)
    configured = values.get(
        "include_attachments",
        parameter.default,
    )
    if configured is Parameter.empty:
        return query
    return replace(
        query,
        include_attachments=query.include_attachments and bool(configured),
    )


def _scope_query(query: SourceQuery) -> dict[str, object]:
    """Keep execution controls out of the stable source membership scope."""

    values = asdict(query)
    values.pop("include_attachments", None)
    filters = json.loads(query.filters_json)
    if isinstance(filters, dict):
        for key in ("include_comments", "build_graph"):
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
