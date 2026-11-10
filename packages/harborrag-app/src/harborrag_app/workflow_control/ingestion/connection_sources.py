"""Prepare ingestion inputs from stored connections."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.importing import import_connectors_once
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.connections import ConnectionResolver, ResolvedConnection
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission, SourceSubmission

from ..control_plane.settings import SettingsService

type SourcePreparation = Callable[..., PreparedSourceSubmission]

logger = logging.getLogger("harborrag.app.workflow_control.ingestion.connection_sources")


@dataclass(frozen=True, slots=True)
class _ResolvedConnectionSettings:
    """The connection scope's revision, plus any overrides to carry onto the run."""

    revision: int
    pdf_engine: str | None
    chunking_max_tokens: int | None


class ConnectionSourceInputBuilder:
    """Resolve the submission's connection, then prepare its provider-neutral input."""

    def __init__(
        self,
        control_plane: Callable[[], ControlPlaneRepositories],
        prepare: SourcePreparation,
    ) -> None:
        self._control_plane = control_plane
        self._prepare = prepare
        self._import_checked = False
        self._import_lock = asyncio.Lock()

    async def __call__(
        self, settings: RuntimeSettings, submission: SourceSubmission
    ) -> PreparedSourceSubmission:
        """Prepare ``submission`` against the connection it names.

        Raises:
            ConnectionNotFoundError: The connection does not exist in the tenant.
            ConnectionUnavailableError: The connection is being deleted.
            ConnectionValidationError: Its stored values fail its schema.
        """
        await self._import_once(settings)
        connection = await self.resolve(submission)
        resolved = await self._resolve_connection_settings(
            connection.snapshot.tenant_id, connection.snapshot.source_key
        )
        return self._prepare(
            settings,
            submission,
            connection=connection,
            settings_revision=resolved.revision,
            pdf_engine=resolved.pdf_engine,
            chunking_max_tokens=resolved.chunking_max_tokens,
        )

    async def _resolve_connection_settings(
        self, tenant_id: str, source_key: str
    ) -> _ResolvedConnectionSettings:
        """The connection scope's revision and any explicit overrides at or above it.

        Only an actual override (``origin`` other than ``default``) is carried
        onto the run: the registry default already matches today's real
        behavior for an untouched connection, which a connector-specific
        chunking profile (e.g. Jira's lower token cap) does not.
        """
        control_plane = self._control_plane()
        if control_plane.setting_overrides is None or control_plane.setting_scope_revisions is None:
            return _ResolvedConnectionSettings(
                revision=0, pdf_engine=None, chunking_max_tokens=None
            )
        project_id, _, connection_id = source_key.partition("/")
        result = await SettingsService(control_plane=self._control_plane).read(
            tenant_id, "connection", f"{project_id}/{connection_id}", effective=True
        )
        values = result.values
        assert isinstance(values, dict)
        pdf_parser = values.get("parsing.pdf_parser")
        max_tokens = values.get("chunking.max_tokens")
        return _ResolvedConnectionSettings(
            revision=result.revision,
            pdf_engine=(
                str(pdf_parser["value"])
                if isinstance(pdf_parser, dict) and pdf_parser.get("origin") != "default"
                else None
            ),
            chunking_max_tokens=(
                int(max_tokens["value"])
                if isinstance(max_tokens, dict) and max_tokens.get("origin") != "default"
                else None
            ),
        )

    async def _import_once(self, settings: RuntimeSettings) -> None:
        """Run the one-time ``connectors.yaml`` import before this process's first submit.

        A failed import is recorded and logged, never retried, so submits of
        stored connections keep working.
        """
        async with self._import_lock:
            if self._import_checked:
                return
            control_plane = self._control_plane()
            try:
                await import_connectors_once(
                    settings.connector_config_path,
                    tenant_id=settings.ingestion_tenant_id,
                    projects=control_plane.projects,
                    connections=control_plane.connections,
                    settings=control_plane.settings,
                )
            except Exception:  # noqa: BLE001 - a broken file must not block stored connections
                logger.warning(
                    "connectors.yaml import failed; submits use the stored connections",
                    exc_info=True,
                )
            self._import_checked = True

    async def resolve(self, submission: SourceSubmission) -> ResolvedConnection:
        """Load and snapshot the connection a submission names."""
        control_plane = self._control_plane()
        if control_plane.connection_revisions is None:
            raise HarborUnavailableError("connection revisions are not configured")
        resolver = ConnectionResolver(control_plane.connections, control_plane.connection_revisions)
        return await resolver.resolve(
            tenant_id=submission.tenant_id,
            source_key=submission.connection_id or submission.connector_name,
        )
