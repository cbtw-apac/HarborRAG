"""Resolve the ``next_ingestion`` settings a run pins at its start."""

from __future__ import annotations

from harborrag_core.config_store import SettingsLevel, revision_label
from harborrag_core.config_store.durations import parse_iso8601_duration_seconds
from harborrag_core.ingestion.run_contracts import RunSettings
from harborrag_core.ports.config_store import ConfigProvider


class RunSettingsResolver:
    """Reads the overridden run settings of one connection."""

    def __init__(self, config: ConfigProvider) -> None:
        self._config = config

    async def for_connection(
        self, *, tenant_id: str, project_id: str, connection_id: str
    ) -> RunSettings:
        """Return the run settings overridden at or above the connection.

        Args:
            tenant_id: Tenant that owns the connection.
            project_id: Project of the connection.
            connection_id: Connection about to run.

        Returns:
            Overridden values only; ``None`` fields leave the boot knob in charge.
        """
        effective = await self._config.effective(
            tenant_id,
            SettingsLevel("connection", project_id=project_id, connection_id=connection_id),
        )
        activity_timeout = _text(effective.override("ingestion.activity_timeout"))
        return RunSettings(
            settings_revision=revision_label(effective.revision),
            pdf_parser=_text(effective.override("parsing.pdf_parser")),
            chunk_max_tokens=_integer(effective.override("chunking.max_tokens")),
            document_concurrency=_integer(effective.override("ingestion.concurrency")),
            activity_timeout_seconds=(
                parse_iso8601_duration_seconds(activity_timeout)
                if activity_timeout is not None
                else None
            ),
            max_attempts=_integer(effective.override("ingestion.max_attempts")),
            batch_size=_integer(effective.override("ingestion.batch_size")),
            max_document_mb=_integer(effective.override("capture.max_document_mb")),
            file_types=_strings(effective.override("capture.file_types")),
            max_pdf_pages=_integer(effective.override("capture.max_pdf_pages")),
            ocr_enabled=_boolean(effective.override("parsing.ocr.enabled")),
            ocr_languages=_strings(effective.override("parsing.ocr.languages")),
            chunk_overlap_tokens=_integer(effective.override("chunking.overlap_tokens")),
            embedding_batch_size=_integer(effective.override("embedding.batch_size")),
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _strings(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)
