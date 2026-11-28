"""Production composition for control-plane repositories and engine services.

Logs name the control database's kind, never its DSN or anything after the scheme.
"""

from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, LiteralString

from harborrag_core.observability.log_events import (
    FailureKind,
    log_event,
    log_failure,
)
from harborrag_core.ports.config_store import ConfigProvider, SettingsStore
from harborrag_core.ports.control_plane import (
    ActivityRepositoryPort,
    ConnectionRepositoryPort,
    ConnectionRevisionRepositoryPort,
    GraphConflictRepositoryPort,
    JobRepositoryPort,
    LeaseRepositoryPort,
    MemberRepositoryPort,
    PendingEffectRepositoryPort,
    ProjectRepositoryPort,
    ProviderRepositoryPort,
    ScheduleRepositoryPort,
    SettingsRepositoryPort,
)
from harborrag_core.ports.conversation import ConversationHistoryRepository
from harborrag_core.ports.memory import MemoryRepository
from harborrag_core.ports.model_catalog import TenantModelCatalogPort
from harborrag_core.ports.secrets import SecretsPort
from harborrag_core.ports.source_freshness import SourceFreshnessReader
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_engine.config import EngineConfig
from harborrag_engine.policy import EnginePolicy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.config_store import CachedConfigProvider

logger = logging.getLogger("harborrag.runtime.composition")

_DEV_DEFAULT_SECRETS_KEY = "harborrag-dev-insecure-default-key"
"""Key used only for a SQLite control database when no key is set.

``secrets_encryption_key`` refuses a missing key for any other control database.
"""
_LOGGED_DATABASE_KIND = re.compile(r"(sqlite|postgres(ql)?)(\+[a-z0-9]+)?")
"""Schemes a log may name; any other DSN prefix is logged as ``unknown``."""
_UNKNOWN_DATABASE_KIND = "unknown"
_UNSTAMPED_SCHEMA_HINT: LiteralString = (
    "The schema exists but is not stamped: stamp alembic_version at the revision the "
    "schema already matches, then upgrade."
)
_KEY_WHITESPACE_HINT: LiteralString = (
    "HARBORRAG_SECRETS_ENCRYPTION_KEY has leading or trailing whitespace and is used as "
    "is, so the same key in HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE would not decrypt the "
    "stored secrets."
)
_DEV_SECRETS_KEY_HINT: LiteralString = (
    "The dev-only default secrets encryption key is in use; set "
    "HARBORRAG_SECRETS_ENCRYPTION_KEY before pointing this process at any persistent or "
    "shared database."
)


@dataclass(frozen=True, slots=True)
class ControlPlaneRepositories:
    """Core-port view of the configured control-plane repositories.

    Attributes:
        projects: Projects.
        connections: Editable connections.
        jobs: Jobs and their event streams.
        activity: Append-only audit feed.
        settings: Single-document workspace settings.
        providers: Model provider registry.
        members: Workspace members and their roles.
        conversation_memory: Chat conversation history.
        secrets: Secret values behind opaque references.
        pending_effects: Durable retry queue for control-plane side effects.
        leases: Named, time-boxed singleton leases.
        graph_conflicts: Graph disagreements awaiting human resolution.
        memories: Canonical long-term memory store; None when not wired.
        model_usage: Model usage accounting; None when not wired.
        model_catalog: Read side of a tenant's own chat models; None when this
            deployment does not wire one, so every tenant uses the process-wide catalog.
        connection_revisions: Immutable connection snapshots; None when not wired.
        config_store: Durable tenant settings and their revision log; None when not wired.
        config: Cached tenant settings; None when not wired.
        source_freshness: Every tenant's source freshness, for operator metrics only;
            None when not wired.
    """

    projects: ProjectRepositoryPort
    connections: ConnectionRepositoryPort
    jobs: JobRepositoryPort
    activity: ActivityRepositoryPort
    settings: SettingsRepositoryPort
    providers: ProviderRepositoryPort
    members: MemberRepositoryPort
    conversation_memory: ConversationHistoryRepository
    secrets: SecretsPort
    pending_effects: PendingEffectRepositoryPort
    leases: LeaseRepositoryPort
    graph_conflicts: GraphConflictRepositoryPort
    memories: MemoryRepository | None = None
    model_usage: ModelUsageRepository | None = None
    model_catalog: TenantModelCatalogPort | None = None
    connection_revisions: ConnectionRevisionRepositoryPort | None = None
    schedules: ScheduleRepositoryPort | None = None
    config_store: SettingsStore | None = None
    config: ConfigProvider | None = None
    source_freshness: SourceFreshnessReader | None = None


@dataclass(slots=True)
class CompositionRoot:
    """Resources assembled for one production runtime process.

    Attributes:
        engine_config: Tenant and environment the engine runs for.
        engine_policy: Engine limits such as its concurrency.
        control_plane: The control-plane repositories; None without a control database.
        control_db: The control database probe: its status, schema revision and scheme.
        mode: Composition mode name, such as ``production``.
    """

    engine_config: EngineConfig = field(default_factory=EngineConfig)
    engine_policy: EnginePolicy = field(default_factory=EnginePolicy)
    control_plane: ControlPlaneRepositories | None = None
    control_db: dict[str, object] = field(default_factory=dict)
    mode: str = "production"
    _control_db_engine: AsyncEngine | None = field(default=None, repr=False)
    _config_provider: CachedConfigProvider | None = field(default=None, repr=False)

    async def start(self) -> None:
        """Start the settings provider.

        Raises:
            HarborUnavailableError: The settings store is unreachable.
        """
        if self._config_provider is not None:
            await self._config_provider.start()

    async def aclose(self) -> None:
        """Stop the settings provider and dispose resources created by :meth:`production`."""
        if self._config_provider is not None:
            await self._config_provider.aclose()
        if self._control_db_engine is not None:
            await self._control_db_engine.dispose()
            self._control_db_engine = None
            log_event(logger, "control_plane.composition_closed")

    @classmethod
    def production(
        cls,
        settings: RuntimeSettings | None = None,
    ) -> CompositionRoot:
        """Migrate, probe, and assemble the configured control-plane database.

        Args:
            settings: Runtime settings; read from the environment when omitted.

        Returns:
            The composition, with its settings provider not yet started.

        Raises:
            HarborConfigurationError: The settings refuse this database, or its
                migrations or probe failed.
        """
        from harborrag_adapters.repositories.database.control_plane.connections import (
            SqlConnectionRepository,
            SqlConnectionRevisionRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.conversation import (
            SqlConversationMemoryRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
            create_session_factory,
        )
        from harborrag_adapters.repositories.database.control_plane.graph_conflicts import (
            SqlGraphConflictRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.jobs import (
            SqlActivityRepository,
            SqlJobRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.leases import (
            SqlLeaseRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.memory import (
            SqlMemoryRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.model_catalog import (
            SqlTenantModelCatalog,
        )
        from harborrag_adapters.repositories.database.control_plane.pending_effects import (
            SqlPendingEffectRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.projects import (
            SqlProjectRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.schedules import (
            SqlScheduleRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.secrets import (
            SqlSecretsRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.usage import (
            SqlModelUsageRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.workspace import (
            SqlMemberRepository,
            SqlProviderRepository,
            SqlSettingsRepository,
        )
        from harborrag_adapters.repositories.database.ingestion_control.source_freshness import (
            SqlSourceFreshnessReader,
        )
        from harborrag_core.contracts.errors import HarborConfigurationError
        from harborrag_runtime.composition.config_store import (
            build_config_provider,
            build_settings_store,
        )
        from harborrag_runtime.config.settings import (
            CONTROL_DB_POOL_RECYCLE_SECONDS,
            RuntimeSettings,
        )

        settings = settings or RuntimeSettings()
        dsn = settings.control_db_url.get_secret_value()
        scheme = dsn.split(":", 1)[0]
        database_kind = _database_kind(dsn)
        log_event(
            logger,
            "control_plane.composition_started",
            environment_kind=settings.env,
            database_kind=database_kind,
        )
        if settings.env != "dev" and scheme.startswith("sqlite"):
            raise HarborConfigurationError(
                "SQLite control databases are not supported when HARBORRAG_ENV is staging or prod; "
                "set HARBORRAG_CONTROL_DB_URL to a production database"
            )
        secrets_key = secrets_encryption_key(settings)
        _migrate(dsn, database_kind)

        control_db = _probe_control_db(dsn)
        if control_db.get("ping") != "ok":
            log_event(
                logger,
                "control_plane.database_not_ready",
                level=logging.ERROR,
                database_kind=database_kind,
            )
            raise HarborConfigurationError(
                "control-plane database probe failed; inspect the startup logs"
            )
        engine = create_control_plane_engine(
            dsn,
            pool_pre_ping=True,
            pool_recycle=CONTROL_DB_POOL_RECYCLE_SECONDS,
            pool_size=settings.control_db_pool_size,
            max_overflow=settings.control_db_max_overflow,
        )
        sessions = create_session_factory(engine)
        config_store = build_settings_store(engine)
        config_provider = build_config_provider(settings, engine=engine, store=config_store)
        repositories = ControlPlaneRepositories(
            projects=SqlProjectRepository(sessions),
            connections=SqlConnectionRepository(sessions),
            connection_revisions=SqlConnectionRevisionRepository(sessions),
            jobs=SqlJobRepository(sessions),
            activity=SqlActivityRepository(sessions),
            settings=SqlSettingsRepository(sessions),
            providers=SqlProviderRepository(sessions),
            members=SqlMemberRepository(sessions),
            conversation_memory=SqlConversationMemoryRepository(sessions),
            secrets=SqlSecretsRepository(sessions, encryption_key=secrets_key),
            pending_effects=SqlPendingEffectRepository(sessions),
            leases=SqlLeaseRepository(sessions),
            graph_conflicts=SqlGraphConflictRepository(sessions),
            memories=SqlMemoryRepository(sessions),
            model_usage=SqlModelUsageRepository(sessions),
            model_catalog=SqlTenantModelCatalog(sessions),
            schedules=SqlScheduleRepository(sessions),
            config_store=config_store,
            config=config_provider,
            source_freshness=SqlSourceFreshnessReader(engine),
        )
        composition = cls(
            control_plane=repositories,
            control_db=control_db,
            _control_db_engine=engine,
            _config_provider=config_provider,
        )
        log_event(
            logger,
            "control_plane.composition_completed",
            database_kind=database_kind,
            probe_status=control_db.get("ping"),
            schema_revision_id=control_db.get("migrations"),
        )
        return composition

    def diagnostics(self) -> dict[str, object]:
        """Return process-safe component health without exposing adapter objects."""
        return {
            "mode": self.mode,
            "runtime": {
                "provider": "production_runtime",
                "ready": self.control_db.get("ping") == "ok",
                "control_db": dict(self.control_db),
            },
            "engine": {
                "tenant": self.engine_config.tenant,
                "environment": self.engine_config.environment,
                "max_concurrency": self.engine_policy.max_concurrency,
            },
        }


def secrets_encryption_key(settings: RuntimeSettings) -> str:
    """Return the key stored secrets are encrypted with.

    ``HARBORRAG_SECRETS_ENCRYPTION_KEY`` wins; otherwise the first line of the file
    ``HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE`` names, such as a Docker or Kubernetes secret.
    The file's line is stripped. The variable is used as is, with a warning when it has
    surrounding whitespace, so keys already in use keep decrypting their secrets.

    Args:
        settings: Runtime settings of this process.

    Returns:
        The configured key, or the dev-only default for a SQLite control database.

    Raises:
        HarborConfigurationError: The key file is unreadable or empty, or no key is set
            and the control database is not SQLite.
    """
    from harborrag_core.contracts.errors import HarborConfigurationError
    from harborrag_runtime.config.settings import is_blank_secret

    dsn = settings.control_db_url.get_secret_value()
    scheme = dsn.split(":", 1)[0]
    encryption_key = settings.secrets_encryption_key
    if encryption_key is not None and not is_blank_secret(encryption_key):
        key = encryption_key.get_secret_value()
        if key != key.strip():
            log_event(
                logger,
                "control_plane.secrets_key_whitespace",
                level=logging.WARNING,
                hint=_KEY_WHITESPACE_HINT,
            )
        return key
    if settings.secrets_encryption_key_file is not None:
        return _read_key_file(settings.secrets_encryption_key_file)
    if not scheme.startswith("sqlite"):
        raise HarborConfigurationError(
            "HARBORRAG_SECRETS_ENCRYPTION_KEY or HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE must be "
            "set when HARBORRAG_CONTROL_DB_URL is not SQLite; the dev-only default key is not "
            "safe for stored secrets"
        )
    log_event(
        logger,
        "control_plane.dev_secrets_key",
        level=logging.WARNING,
        hint=_DEV_SECRETS_KEY_HINT,
        database_kind=_database_kind(dsn),
    )
    return _DEV_DEFAULT_SECRETS_KEY


def _read_key_file(path: Path) -> str:
    """Return the first line of the key file.

    Args:
        path: The file ``HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE`` names.

    Returns:
        The file's first line, stripped.

    Raises:
        HarborConfigurationError: The file cannot be read or its first line is blank.
    """
    from harborrag_core.contracts.errors import HarborConfigurationError

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise HarborConfigurationError(
            f"HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE cannot be read: {path}"
        ) from error
    key = lines[0].strip() if lines else ""
    if not key:
        raise HarborConfigurationError(f"HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE is empty: {path}")
    return key


def _failed_database_status(dsn: str, error: str) -> dict[str, object]:
    """Return the probe result of a database that could not be read."""
    return {"ping": "failed", "error": error, "scheme": dsn.split(":", 1)[0]}


def _database_kind(dsn: str) -> str:
    """Return the DSN's scheme when it names a known database, else ``unknown``.

    A malformed DSN can hold a user name or password before its first colon.
    """
    scheme = dsn.split(":", 1)[0]
    return scheme if _LOGGED_DATABASE_KIND.fullmatch(scheme) else _UNKNOWN_DATABASE_KIND


def _migrate(dsn: str, database_kind: str) -> None:
    """Run the control-plane migrations, logging a failure before it is raised.

    Args:
        dsn: Control database URL.
        database_kind: The DSN's scheme as logs may name it.

    Raises:
        HarborConfigurationError: The database is ahead of this build, or the
            migrations failed.
    """
    from harborrag_adapters.repositories.database.control_plane.migrations import (
        run_migrations,
    )
    from harborrag_adapters.repositories.errors import SchemaAheadOfBuildError
    from harborrag_core.contracts.errors import HarborConfigurationError

    try:
        run_migrations(dsn)
    except SchemaAheadOfBuildError as exc:
        log_failure(
            logger,
            "control_plane.schema_ahead_of_build",
            exc,
            FailureKind.CLASSIFIED,
            database_kind=database_kind,
            schema_revision_id=exc.database_revision,
            build_revision_id=exc.build_head,
        )
        raise HarborConfigurationError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - wrap adapter failures at the boundary
        log_event(
            logger,
            "control_plane.migration_failed",
            level=logging.ERROR,
            hint=_migration_failure_hint(exc),
            database_kind=database_kind,
            error=exc,
        )
        raise HarborConfigurationError(
            "control-plane migrations failed; inspect the startup logs"
        ) from exc


def _migration_failure_hint(exc: Exception) -> LiteralString | None:
    """Name the one migration failure that looks like a bug but is a bookkeeping gap.

    "table X already exists" on the first migration means the schema was built without
    Alembic recording it, so the runner replays from base and collides with the tables
    that are already there. Nothing is corrupt and no data is lost: the version table
    just needs stamping at the revision the schema already matches.
    """
    return _UNSTAMPED_SCHEMA_HINT if "already exists" in str(exc) else None


def _probe_control_db(dsn: str) -> dict[str, object]:
    """Probe connectivity and the migration stamp using a short-lived engine."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _probe() -> dict[str, object]:
        engine = create_async_engine(dsn, hide_parameters=True)
        try:
            async with engine.connect() as connection:
                version = (
                    await connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                ).scalar()
            return {
                "ping": "ok",
                "migrations": version,
                "scheme": dsn.split(":", 1)[0],
            }
        finally:
            await engine.dispose()

    def _run() -> dict[str, object]:
        try:
            return asyncio.run(_probe())
        except Exception as exc:  # noqa: BLE001 - diagnostics must remain available
            log_failure(
                logger,
                "control_plane.database_probe_failed",
                exc,
                FailureKind.EXPECTED,
                database_kind=_database_kind(dsn),
            )
            return _failed_database_status(dsn, f"probe failed ({type(exc).__name__})")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()
