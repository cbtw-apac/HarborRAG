"""Prepare ingestion inputs from stored connections."""

from __future__ import annotations

from collections.abc import Callable

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.importing import import_connectors_once
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.connections import ConnectionResolver, ResolvedConnection
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission, SourceSubmission

type SourcePreparation = Callable[..., PreparedSourceSubmission]


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
        return self._prepare(settings, submission, connection=connection)

    async def _import_once(self, settings: RuntimeSettings) -> None:
        """Run the one-time ``connectors.yaml`` import before this process's first submit."""
        if self._import_checked:
            return
        control_plane = self._control_plane()
        await import_connectors_once(
            settings.connector_config_path,
            projects=control_plane.projects,
            connections=control_plane.connections,
            settings=control_plane.settings,
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
