"""Default gateway composition; applications may inject another implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.ingestion_contracts import (
    IngestionGateway,
    PreparedSourceSubmission,
    SourceSubmission,
)
from harborrag_runtime.temporal.optional import load_temporal_attribute

from .source_submission import SourceSubmissionDefaults, prepare_source_submission

if TYPE_CHECKING:
    from harborrag_runtime.temporal.client import IngestionTemporalClient


@dataclass(frozen=True, slots=True)
class IngestionGatewayDescription:
    provider: str
    health_timeout_seconds: float
    details: Mapping[str, object]


def describe_ingestion_gateway(settings: RuntimeSettings) -> IngestionGatewayDescription:
    config = TemporalRuntimeConfig.from_settings(settings)
    return IngestionGatewayDescription(
        provider="temporal",
        health_timeout_seconds=config.health_timeout_seconds,
        details={"target": config.connection.target, "namespace": config.connection.namespace},
    )


def prepare_configured_source_submission(
    settings: RuntimeSettings,
    submission: SourceSubmission,
    *,
    environment: Mapping[str, str] | None = None,
) -> PreparedSourceSubmission:
    """Translate the shared ingestion batch policy at composition only.

    Reads only the ingestion section: this runs on the direct path too, which
    never connects to Temporal and must not be failed by its deployment config.
    """

    defaults = TemporalRuntimeConfig.ingestion_from_settings(settings)
    return prepare_source_submission(
        settings,
        submission,
        environment=environment,
        defaults=SourceSubmissionDefaults(
            batch_size=defaults.batch_size,
            document_concurrency=defaults.document_concurrency,
        ),
    )


async def connect_temporal_client(config: TemporalRuntimeConfig) -> IngestionTemporalClient:
    """Legacy factory retained while optional SDK imports remain at the boundary."""

    client_type = cast(
        "type[IngestionTemporalClient]",
        load_temporal_attribute("harborrag_runtime.temporal.client", "IngestionTemporalClient"),
    )
    return await client_type.connect(config)


async def connect_ingestion_gateway(settings: RuntimeSettings) -> IngestionGateway:
    from harborrag_runtime.temporal.gateway import TemporalIngestionGateway

    return TemporalIngestionGateway(
        await connect_temporal_client(TemporalRuntimeConfig.from_settings(settings))
    )
