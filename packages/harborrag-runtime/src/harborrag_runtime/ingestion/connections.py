"""Build connectors from stored connections, per ingestion run.

Handles:
- ``ConnectionResolver``: load a connection by ``source_key``, validate it and
  save the immutable snapshot the run will use
- ``build_connector``: the one place a connector is constructed, shared by the
  workers and the connection test
- ``ConnectorPool``: per-run connectors in a worker, built on first use from the
  run's snapshot with secrets resolved inside the activity
- ``ConnectionHealth``: ``needs_reauth`` on credential failures, ``active`` again
  after a run authenticates

Does NOT read ``connectors.yaml``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from inspect import Parameter, signature
from typing import Any

from harborrag_adapters.connectors.file_filters import normalize_extension
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.connectors.registry import connector_registry
from harborrag_adapters.parsers import HarborParserRegistry
from harborrag_core.contracts.errors import HarborConflictError, HarborValidationError
from harborrag_core.domain.connection import Connection
from harborrag_core.domain.connection_snapshot import ConnectionSnapshot
from harborrag_core.ingestion.errors import SourceAuthenticationError, SourceAuthorizationError
from harborrag_core.ports.control_plane import (
    ConnectionRepositoryPort,
    ConnectionRevisionRepositoryPort,
)
from harborrag_runtime.config.connectors.models import (
    ConnectorConfigBuilder,
    SourceTypeSpec,
    source_types,
)
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.connections import SecretResolver

logger = logging.getLogger("harborrag.runtime.ingestion.connections")

DEFAULT_POOL_CAPACITY = 32


class ConnectionNotFoundError(HarborValidationError):
    """An ingestion names a connection that does not exist in the tenant."""

    error_code = "reference_not_found"


class ConnectionUnavailableError(HarborConflictError):
    """The connection exists but cannot start a run in its current state."""

    error_code = "invalid_state"


@dataclass(frozen=True, slots=True)
class ResolvedConnection:
    """A validated connection and the snapshot a run starts from."""

    snapshot: ConnectionSnapshot
    spec: SourceTypeSpec

    @property
    def fingerprint(self) -> str:
        """Content fingerprint every activity of the run checks against."""
        return self.snapshot.fingerprint

    @property
    def provider(self) -> str:
        """Connector provider name, also the run's ``connector_type``."""
        return self.spec.provider

    def query_filters(self) -> dict[str, Any]:
        """Config values that travel in the run's query filters, e.g. Jira ``jql``."""
        return self.spec.query_filters(self.snapshot.config)

    def include_attachments(self) -> bool:
        """Whether the connection allows attachments; the dataclass default when unset."""
        if "include_attachments" in self.snapshot.config:
            return bool(self.snapshot.config["include_attachments"])
        parameter = signature(source_types.provider_factory(self.spec.source_type)).parameters.get(
            "include_attachments"
        )
        if parameter is None or parameter.default is Parameter.empty:
            return True
        return bool(parameter.default)


class ConnectionResolver:
    """Turn a stored connection into the snapshot one ingestion run uses."""

    def __init__(
        self,
        connections: ConnectionRepositoryPort,
        revisions: ConnectionRevisionRepositoryPort,
    ) -> None:
        self._connections = connections
        self._revisions = revisions

    async def resolve(self, *, tenant_id: str, source_key: str) -> ResolvedConnection:
        """Load, validate and snapshot the connection a run will use.

        Args:
            tenant_id: Tenant that owns the connection.
            source_key: Key ingestion addresses the connection by.

        Returns:
            The validated connection and its saved snapshot.

        Raises:
            ConnectionNotFoundError: No such connection in the tenant.
            ConnectionUnavailableError: The connection is being deleted.
            ConnectionValidationError: Its stored values fail its source type schema.
        """
        connection = await self._connections.get_by_source_key(
            tenant_id=tenant_id, source_key=source_key
        )
        if connection is None:
            raise ConnectionNotFoundError(f"connection {source_key!r} does not exist")
        return await self.resolve_connection(connection)

    async def resolve_connection(self, connection: Connection) -> ResolvedConnection:
        """Validate and snapshot an already loaded connection."""
        if connection.status == "deleting":
            raise ConnectionUnavailableError(f"connection {connection.ref!r} is being deleted")
        spec = source_types.get(connection.source_type)
        validated = spec.validate(connection.credentials, connection.config, connection.tuning)
        # TODO: merge tenant- and project-level connectors.<source_type>.* settings below these.
        snapshot = ConnectionSnapshot(
            tenant_id=connection.tenant_id,
            source_key=connection.source_key,
            source_type=spec.source_type,
            credentials=validated.credentials,
            config=validated.config,
            tuning=validated.tuning,
        )
        await self._revisions.save(snapshot)
        return ResolvedConnection(snapshot, spec)


def build_connector(
    snapshot: ConnectionSnapshot,
    secret_values: Mapping[str, str],
    *,
    parser: HarborParserRegistry | None = None,
    rate_limiter: ConnectorRateLimiter | None = None,
) -> HarborConnector:
    """Construct the connector a snapshot describes.

    Args:
        snapshot: The run's connection snapshot.
        secret_values: Plaintext for each credential field held as a reference.
        parser: Attachment parser; connectors that accept one run without it otherwise.
        rate_limiter: Shared limiter for connectors that use one.

    Returns:
        A ready-to-connect connector facade.

    Raises:
        ConnectorConfigurationError: The values are rejected or allow unparseable files.
        ConnectionValidationError: A secret reference has no resolved value.
    """
    return build_connector_from_parts(
        snapshot.source_type,
        credentials=snapshot.credentials,
        config=snapshot.config,
        tuning=snapshot.tuning,
        secret_values=secret_values,
        parser=parser,
        rate_limiter=rate_limiter,
    )


def build_connector_from_parts(  # noqa: PLR0913 - the parts of one connection
    source_type: str,
    *,
    credentials: Mapping[str, Any],
    config: Mapping[str, Any],
    tuning: Mapping[str, Any],
    secret_values: Mapping[str, str],
    parser: HarborParserRegistry | None = None,
    rate_limiter: ConnectorRateLimiter | None = None,
) -> HarborConnector:
    """Construct a connector from validated parts; credentials may hold raw values.

    The connection test uses this for form values that were never saved.
    """
    spec = source_types.get(source_type)
    config_object = (
        ConnectorConfigBuilder.for_source_type(spec.source_type)
        .credentials(credentials)
        .config(config)
        .tuning(tuning)
        .secret_values(secret_values)
        .build()
    )
    if parser is not None:
        _require_parseable_extensions(spec.source_type, config, parser)
    available: dict[str, object | None] = {
        "attachment_parser": parser,
        "rate_limiter": rate_limiter,
    }
    definition = connector_registry.get_definition(spec.provider)
    kwargs = {
        argument: available[dependency]
        for argument, dependency in definition.constructor_dependencies.items()
        if available.get(dependency) is not None
    }
    return HarborConnector(spec.provider, config=config_object, **kwargs)


def _require_parseable_extensions(
    source_type: str, config: Mapping[str, Any], parser: HarborParserRegistry
) -> None:
    """Reject an ``allowed_extensions`` entry that no parser family can read."""
    allowed = config.get("allowed_extensions") or ()
    unsupported = sorted(
        {
            normalize_extension(str(extension))
            for extension in allowed
            if not parser.supports_extension(str(extension))
        }
    )
    if unsupported:
        raise ConnectorConfigurationError(
            f"The {source_type} connection allows extensions no parser supports: "
            f"{', '.join(unsupported)}"
        )


class ConnectorPool:
    """Per-run connectors for one worker, bounded and closed on eviction.

    Entries are keyed by ``(tenant_id, fingerprint, task_id)``: every activity of
    one run shares a connector, while a later run, or the same run after an
    edit, builds its own from the snapshot it started with.
    """

    def __init__(
        self,
        *,
        revisions: ConnectionRevisionRepositoryPort,
        secrets: SecretResolver,
        parser: HarborParserRegistry | None = None,
        rate_limiter: ConnectorRateLimiter | None = None,
        capacity: int = DEFAULT_POOL_CAPACITY,
    ) -> None:
        self._revisions = revisions
        self._secrets = secrets
        self._parser = parser
        self._rate_limiter = rate_limiter
        self._capacity = capacity
        self._entries: OrderedDict[tuple[str, str, str], HarborConnector] = OrderedDict()
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def connector(
        self, *, tenant_id: str, task_id: str, configuration_fingerprint: str
    ) -> HarborConnector:
        """Return the run's connector, building it from its snapshot on first use.

        Raises:
            ConnectorConfigurationError: The snapshot is unknown or its values are rejected.
            SecretUnavailableError: A referenced secret cannot be read on this worker.
        """
        key = (tenant_id, configuration_fingerprint, task_id)
        if key in self._entries:
            self._entries.move_to_end(key)
            return self._entries[key]
        async with self._locks.setdefault(key, asyncio.Lock()):
            connector = self._entries.get(key)
            if connector is None:
                connector = await self._build(tenant_id, configuration_fingerprint)
                self._entries[key] = connector
                await self._evict()
        self._locks.pop(key, None)
        return connector

    async def snapshot(
        self, *, tenant_id: str, configuration_fingerprint: str
    ) -> ConnectionSnapshot:
        """Return the stored snapshot behind a fingerprint.

        Raises:
            ConnectorConfigurationError: No snapshot has this fingerprint.
        """
        snapshot = await self._revisions.get(
            tenant_id=tenant_id, fingerprint=configuration_fingerprint
        )
        if snapshot is None:
            raise ConnectorConfigurationError(
                f"connection revision {configuration_fingerprint!r} is unknown"
            )
        return snapshot

    async def aclose(self) -> None:
        """Close every pooled connector."""
        entries = list(self._entries.values())
        self._entries.clear()
        await asyncio.gather(*(asyncio.to_thread(c.close) for c in entries), return_exceptions=True)

    async def _build(self, tenant_id: str, fingerprint: str) -> HarborConnector:
        """Load the snapshot, resolve its secrets and construct the connector."""
        snapshot = await self.snapshot(tenant_id=tenant_id, configuration_fingerprint=fingerprint)
        secret_values = await self._secrets.resolve_credentials(
            snapshot.credentials, tenant_id=tenant_id
        )
        connector = await asyncio.to_thread(
            build_connector,
            snapshot,
            secret_values,
            parser=self._parser,
            rate_limiter=self._rate_limiter,
        )
        logger.info(
            "Connection connector built source_type=%s fingerprint=%s",
            snapshot.source_type,
            fingerprint,
        )
        return connector

    async def _evict(self) -> None:
        """Close the least recently used connectors beyond capacity."""
        while len(self._entries) > self._capacity:
            _, evicted = self._entries.popitem(last=False)
            try:
                await asyncio.to_thread(evicted.close)
            except Exception:
                logger.warning("Closing an evicted connector failed", exc_info=True)


def require_fingerprint(fingerprint: str | None) -> str:
    """Return a run's connection fingerprint.

    Raises:
        ConnectorConfigurationError: The run predates connection revisions.
    """
    if not fingerprint:
        raise ConnectorConfigurationError("the run has no connection revision; start a new sync")
    return fingerprint


class ConnectionHealth:
    """Keep a connection's status in step with what its runs see."""

    def __init__(self, connections: ConnectionRepositoryPort | None) -> None:
        self._connections = connections

    async def record_failure(
        self, *, tenant_id: str, source_key: str, error: BaseException
    ) -> None:
        """Mark the connection ``needs_reauth`` when ``error`` rejects its credentials."""
        if self._connections is None or not isinstance(
            error, (SourceAuthenticationError, SourceAuthorizationError)
        ):
            return
        try:
            await self._connections.set_status(
                tenant_id=tenant_id,
                source_key=source_key,
                status="needs_reauth",
                detail={"error_category": "auth_invalid"},
                only_from=frozenset({"active", "needs_reauth"}),
            )
        except Exception:
            logger.warning("Recording needs_reauth failed", exc_info=True)

    async def record_success(self, *, tenant_id: str, source_key: str) -> None:
        """Mark a ``needs_reauth`` connection ``active`` once a run authenticates again."""
        if self._connections is None:
            return
        try:
            await self._connections.set_status(
                tenant_id=tenant_id,
                source_key=source_key,
                status="active",
                only_from=frozenset({"needs_reauth"}),
            )
        except Exception:
            logger.warning("Recording a healthy connection failed", exc_info=True)
