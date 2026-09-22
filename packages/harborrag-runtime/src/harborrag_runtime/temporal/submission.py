"""Compatibility construction of Temporal-specific source workflow inputs."""

from __future__ import annotations

from collections.abc import Mapping

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.execution.gateway import prepare_configured_source_submission
from harborrag_runtime.ingestion_contracts import SourceSubmission

from .gateway import to_temporal_source
from .schemas import SourceIngestionInput


def build_source_input(
    settings: RuntimeSettings,
    submission: SourceSubmission,
    *,
    environment: Mapping[str, str] | None = None,
) -> SourceIngestionInput:
    return to_temporal_source(
        prepare_configured_source_submission(settings, submission, environment=environment)
    )


__all__ = ["SourceSubmission", "build_source_input"]
