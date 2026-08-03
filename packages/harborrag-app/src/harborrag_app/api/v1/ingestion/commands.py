"""Translate public ingestion requests into application commands."""

from __future__ import annotations

from harborrag_app.workflow_control.ingestion_models import IngestionCreateCommand

from .schemas import IngestionCreateRequest


def build_ingestion_command(request: IngestionCreateRequest) -> IngestionCreateCommand:
    """Map the validated transport model to the application facade contract."""

    return IngestionCreateCommand(
        tenant_id=request.tenant,
        connection_id=request.connection_id,
        force_reprocess=request.mode == "force",
        public_request=request.model_dump(mode="json"),
    )
