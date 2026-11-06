"""Resolve the ``next_ingestion`` settings a run pins at its start."""

from __future__ import annotations

from harborrag_core.config_store import SettingsLevel, revision_label
from harborrag_core.ports.config_store import ConfigProvider
from harborrag_runtime.ingestion_contracts import RunSettings


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
        return RunSettings(
            settings_revision=revision_label(effective.revision),
            pdf_parser=_text(effective.override("parsing.pdf_parser")),
            chunk_max_tokens=_integer(effective.override("chunking.max_tokens")),
            document_concurrency=_integer(effective.override("ingestion.concurrency")),
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
