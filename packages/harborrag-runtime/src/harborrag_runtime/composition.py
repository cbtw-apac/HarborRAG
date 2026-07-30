"""Production composition for control-plane repositories and engine services."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from harborrag_core.ports.control_plane import (
    ActivityRepositoryPort,
    JobRepositoryPort,
    MemberRepositoryPort,
    ProjectRepositoryPort,
    ProviderRepositoryPort,
    SettingsRepositoryPort,
    SourceRepositoryPort,
)
from harborrag_core.ports.secrets import SecretsPort
from harborrag_engine.config import EngineConfig
from harborrag_engine.policy import EnginePolicy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings


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
    secrets: SecretsPort


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

    @classmethod
    def production(
        cls,
        settings: RuntimeSettings | None = None,
    ) -> CompositionRoot:
        """Migrate, probe, and assemble the configured control-plane database."""

        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
            create_session_factory,
        )
        from harborrag_adapters.repositories.database.control_plane.jobs import (
            SqlActivityRepository,
            SqlJobRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.migrations import (
            run_migrations,
        )
        from harborrag_adapters.repositories.database.control_plane.projects import (
            SqlProjectRepository,
            SqlSourceRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.workspace import (
            SqlMemberRepository,
            SqlProviderRepository,
            SqlSettingsRepository,
        )
        from harborrag_adapters.repositories.secrets.file import FileSecretsRepository
        from harborrag_core.contracts.errors import HarborConfigurationError
        from harborrag_runtime.config.settings import (
            DEFAULT_CONTROL_DB_URL,
            RuntimeSettings,
        )

        settings = settings or RuntimeSettings()
        dsn = settings.control_db_url
        if settings.env == "prod" and dsn == DEFAULT_CONTROL_DB_URL:
            raise HarborConfigurationError(
                "control_db_url is not set when HARBORRAG_ENV=prod: refusing to "
                "boot against the default local SQLite database; set "
                "HARBORRAG_CONTROL_DB_URL explicitly"
            )

        try:
            run_migrations(dsn)
        except Exception as exc:  # noqa: BLE001 - health reports boot degradation
            return cls(
                control_db=_failed_database_status(
                    dsn,
                    f"migrations failed: {exc}",
                )
            )

        control_db = _probe_control_db(dsn)
        engine = create_control_plane_engine(dsn)
        sessions = create_session_factory(engine)
        repositories = ControlPlaneRepositories(
            projects=SqlProjectRepository(sessions),
            sources=SqlSourceRepository(sessions),
            jobs=SqlJobRepository(sessions),
            activity=SqlActivityRepository(sessions),
            settings=SqlSettingsRepository(sessions),
            providers=SqlProviderRepository(sessions),
            members=SqlMemberRepository(sessions),
            secrets=FileSecretsRepository(settings.secrets_file_path),
        )
        return cls(
            control_plane=repositories,
            control_db=control_db,
            _control_db_engine=engine,
        )

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
            return _failed_database_status(dsn, str(exc))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()
