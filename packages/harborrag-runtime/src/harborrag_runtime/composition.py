"""Production composition for control-plane repositories and engine services."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from harborrag_core.ports.agent_runs import AgentRunRepository
from harborrag_core.ports.control_plane import (
    ActivityRepositoryPort,
    JobRepositoryPort,
    LeaseRepositoryPort,
    MemberRepositoryPort,
    PendingEffectRepositoryPort,
    ProjectRepositoryPort,
    ProviderRepositoryPort,
    SettingsRepositoryPort,
    SourceRepositoryPort,
)
from harborrag_core.ports.conversation import ConversationRepository
from harborrag_core.ports.secrets import SecretsPort
from harborrag_engine.config import EngineConfig
from harborrag_engine.policy import EnginePolicy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.composition")

# Only reachable when the control DB is SQLite and HARBORRAG_SECRETS_ENCRYPTION_KEY is
# unset; RuntimeSettings.validate_secret_urls() rejects a missing key outright for any
# non-SQLite control database (including dev), and always in prod.
_DEV_DEFAULT_SECRETS_KEY = "harborrag-dev-insecure-default-key"


@dataclass(frozen=True, slots=True)
class ControlPlaneRepositories:
    """Core-port view of the configured control-plane repositories."""

    projects: ProjectRepositoryPort
    sources: SourceRepositoryPort
    jobs: JobRepositoryPort
    activity: ActivityRepositoryPort
    settings: SettingsRepositoryPort
    providers: ProviderRepositoryPort
    members: MemberRepositoryPort
    conversation_memory: ConversationRepository
    agent_runs: AgentRunRepository
    secrets: SecretsPort
    pending_effects: PendingEffectRepositoryPort
    leases: LeaseRepositoryPort


@dataclass(slots=True)
class CompositionRoot:
    """Resources assembled for one production runtime process."""

    engine_config: EngineConfig = field(default_factory=EngineConfig)
    engine_policy: EnginePolicy = field(default_factory=EnginePolicy)
    control_plane: ControlPlaneRepositories | None = None
    control_db: dict[str, Any] = field(default_factory=dict)
    mode: str = "production"
    _control_db_engine: AsyncEngine | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Dispose resources created by :meth:`production`."""

        if self._control_db_engine is not None:
            await self._control_db_engine.dispose()
            self._control_db_engine = None
            logger.info("Control-plane composition resources closed")

    @classmethod
    def production(
        cls,
        settings: RuntimeSettings | None = None,
    ) -> CompositionRoot:
        """Migrate, probe, and assemble the configured control-plane database."""

        from harborrag_adapters.repositories.database.control_plane.agent_runs import (
            SqlAgentRunRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.conversation import (
            SqlConversationMemoryRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
            create_session_factory,
        )
        from harborrag_adapters.repositories.database.control_plane.jobs import (
            SqlActivityRepository,
            SqlJobRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.leases import (
            SqlLeaseRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.migrations import (
            run_migrations,
        )
        from harborrag_adapters.repositories.database.control_plane.pending_effects import (
            SqlPendingEffectRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.projects import (
            SqlProjectRepository,
            SqlSourceRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.secrets import (
            SqlSecretsRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.workspace import (
            SqlMemberRepository,
            SqlProviderRepository,
            SqlSettingsRepository,
        )
        from harborrag_core.contracts.errors import HarborConfigurationError
        from harborrag_runtime.config.settings import RuntimeSettings

        settings = settings or RuntimeSettings()
        dsn = settings.control_db_url.get_secret_value()
        scheme = dsn.split(":", 1)[0]
        logger.info(
            "Control-plane composition started environment=%s database_scheme=%s",
            settings.env,
            scheme,
        )
        if settings.env == "prod" and scheme.startswith("sqlite"):
            raise HarborConfigurationError(
                "SQLite control databases are not supported when HARBORRAG_ENV=prod; "
                "set HARBORRAG_CONTROL_DB_URL to a production database"
            )

        try:
            run_migrations(dsn)
        except Exception as exc:  # noqa: BLE001 - wrap adapter failures at the boundary
            logger.error(
                "Control-plane migrations failed database_scheme=%s error_type=%s%s",
                scheme,
                type(exc).__name__,
                _migration_failure_hint(exc),
            )
            raise HarborConfigurationError(
                "control-plane migrations failed; inspect the startup logs"
            ) from exc

        control_db = _probe_control_db(dsn)
        if control_db.get("ping") != "ok":
            logger.error(
                "Control-plane probe failed database_scheme=%s",
                scheme,
            )
            raise HarborConfigurationError(
                "control-plane database probe failed; inspect the startup logs"
            )
        engine = create_control_plane_engine(dsn)
        sessions = create_session_factory(engine)
        if settings.secrets_encryption_key is not None:
            secrets_key = settings.secrets_encryption_key.get_secret_value()
        else:
            secrets_key = _DEV_DEFAULT_SECRETS_KEY
            logger.warning(
                "Using the dev-only default secrets encryption key against a SQLite "
                "control database; set HARBORRAG_SECRETS_ENCRYPTION_KEY before pointing "
                "this process at any persistent or shared database"
            )
        repositories = ControlPlaneRepositories(
            projects=SqlProjectRepository(sessions),
            sources=SqlSourceRepository(sessions),
            jobs=SqlJobRepository(sessions),
            activity=SqlActivityRepository(sessions),
            settings=SqlSettingsRepository(sessions),
            providers=SqlProviderRepository(sessions),
            members=SqlMemberRepository(sessions),
            conversation_memory=SqlConversationMemoryRepository(sessions),
            agent_runs=SqlAgentRunRepository(sessions),
            secrets=SqlSecretsRepository(sessions, encryption_key=secrets_key),
            pending_effects=SqlPendingEffectRepository(sessions),
            leases=SqlLeaseRepository(sessions),
        )
        composition = cls(
            control_plane=repositories,
            control_db=control_db,
            _control_db_engine=engine,
        )
        logger.info(
            "Control-plane composition completed database_scheme=%s ready=%s migration=%s",
            scheme,
            control_db.get("ping") == "ok",
            control_db.get("migrations"),
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


def _failed_database_status(dsn: str, error: str) -> dict[str, Any]:
    return {"ping": "failed", "error": error, "scheme": dsn.split(":", 1)[0]}


def _migration_failure_hint(exc: Exception) -> str:
    """Name the one migration failure that looks like a bug but is a bookkeeping gap.

    "table X already exists" on the first migration means the schema was built without
    Alembic recording it, so the runner replays from base and collides with the tables
    that are already there. Nothing is corrupt and no data is lost -- the version table
    just needs stamping at the revision the schema already matches.
    """

    if "already exists" not in str(exc):
        return ""
    return (
        " hint=the schema exists but is not stamped; "
        "stamp alembic_version at the revision the schema already matches, then upgrade"
    )


def _probe_control_db(dsn: str) -> dict[str, Any]:
    """Probe connectivity and the migration stamp using a short-lived engine."""

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _probe() -> dict[str, Any]:
        engine = create_async_engine(dsn)
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

    def _run() -> dict[str, Any]:
        try:
            return asyncio.run(_probe())
        except Exception as exc:  # noqa: BLE001 - diagnostics must remain available
            logger.warning(
                "Control-plane database probe raised database_scheme=%s error_type=%s",
                dsn.split(":", 1)[0],
                type(exc).__name__,
            )
            return _failed_database_status(dsn, f"probe failed ({type(exc).__name__})")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()
