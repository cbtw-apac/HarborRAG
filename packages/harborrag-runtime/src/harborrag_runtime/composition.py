"""Composition roots: mock/local wiring and env-driven production wiring (ST8).

Runtime is the only layer allowed to construct adapters (deps-check enforces
app -> {core, engine, runtime} only), so control-plane repositories and the
secrets/event fakes are assembled here and exposed as core port types.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.parsers.text import TextParser
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ports.control_plane import (
    ActivityRepositoryPort,
    JobRepositoryPort,
    MemberRepositoryPort,
    ProjectRepositoryPort,
    ProviderRepositoryPort,
    SettingsRepositoryPort,
    SourceRepositoryPort,
)
from harborrag_core.ports.events import EventBusPort
from harborrag_core.ports.secrets import SecretsPort
from harborrag_engine.builder import EngineBuilder

from harborrag_runtime.services.base import BaseRuntimeService
from harborrag_runtime.services.mock import MockRuntimeService

if TYPE_CHECKING:
    from harborrag_runtime.settings import RuntimeSettings


@dataclass(frozen=True, slots=True)
class _MockIngestionSummary:
    """Summary shape for the deterministic composition smoke pipeline."""

    discovered: int
    loaded: int
    parsed: int
    indexed: int


class _MockIngestionConnector(BaseConnector):
    """In-memory connector yielding one canned record for health checks."""

    provider_name = "mock_runtime"

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        yield SourceRecord(
            id="mock://composition/1",
            source_type="text/plain",
            locator="mock://composition/1",
        )

    def load(self, record: SourceRecord) -> RawDocument:
        return RawDocument(
            id=record.id,
            source=record.locator,
            content="HarborRAG mock ingestion content",
            content_type="text/plain",
        )


@dataclass(slots=True)
class _MockIngestionShim:
    """Reduced connector-to-parser pipeline used by local health checks."""

    connector: BaseConnector
    parser: TextParser
    _summary: _MockIngestionSummary = field(
        default_factory=lambda: _MockIngestionSummary(0, 0, 0, 0)
    )

    def run_once(self) -> list[RawDocument]:
        documents: list[RawDocument] = []
        for record in self.connector.discover():
            raw = self.connector.load(record)
            self.parser.parse(raw)
            documents.append(raw)
        count = len(documents)
        self._summary = _MockIngestionSummary(count, count, count, 0)
        return documents

    def summarize(self) -> _MockIngestionSummary:
        return self._summary


@dataclass(slots=True)
class ControlPlaneRepositories:
    """Port-typed bundle of control-plane repositories.

    Typing these fields as the core Protocols makes mypy verify that the
    SQLAlchemy implementations conform (ST5 DoD).
    """

    projects: ProjectRepositoryPort
    sources: SourceRepositoryPort
    jobs: JobRepositoryPort
    activity: ActivityRepositoryPort
    settings: SettingsRepositoryPort
    providers: ProviderRepositoryPort
    members: MemberRepositoryPort


@dataclass(slots=True)
class CompositionRoot:
    engine_builder: EngineBuilder
    runtime_service: BaseRuntimeService = field(default_factory=MockRuntimeService)
    control_plane: ControlPlaneRepositories | None = None
    secrets: SecretsPort | None = None
    events: EventBusPort | None = None
    mode: str = "local"

    @classmethod
    def local(cls) -> CompositionRoot:
        return cls(engine_builder=EngineBuilder())

    @classmethod
    def production(cls, settings: RuntimeSettings | None = None) -> CompositionRoot:
        """Env-driven composition: control-plane DB (migrated), repositories,
        dev secrets/event fakes, and the production runtime service.

        Heavy imports stay inside the method so the bare CLI install (no
        [production]/[control-plane] extras) never pays for them. Callers in
        an event loop should invoke this via asyncio.to_thread (migrations
        and the boot probe drive their own loops).
        """
        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
            create_session_factory,
        )
        from harborrag_adapters.repositories.database.control_plane.migrations import (
            run_migrations,
        )
        from harborrag_adapters.repositories.database.control_plane.repositories import (
            SqlActivityRepository,
            SqlJobRepository,
            SqlMemberRepository,
            SqlProjectRepository,
            SqlProviderRepository,
            SqlSettingsRepository,
            SqlSourceRepository,
        )
        from harborrag_core.contracts.errors import HarborConfigurationError
        from harborrag_core.testing.fakes import FakeEventBus, FakeSecrets

        from harborrag_runtime.services.runtime_service import ProductionRuntimeService
        from harborrag_runtime.settings import DEFAULT_CONTROL_DB_URL, RuntimeSettings

        settings = settings or RuntimeSettings()
        dsn = settings.control_db_url
        if settings.env == "prod" and dsn == DEFAULT_CONTROL_DB_URL:
            raise HarborConfigurationError(
                "control_db_url is not set when HARBORRAG_ENV=prod: refusing to "
                "boot production composition against the default local SQLite "
                "database; set HARBORRAG_CONTROL_DB_URL explicitly"
            )
        try:
            run_migrations(dsn)
        except Exception as exc:  # noqa: BLE001 - boot degraded, readyz reports it
            return cls(
                engine_builder=EngineBuilder(),
                runtime_service=ProductionRuntimeService(
                    control_db={
                        "ping": "failed",
                        "error": f"migrations failed: {exc}",
                        "scheme": dsn.split(":", 1)[0],
                    }
                ),
                mode="production",
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
        )
        return cls(
            engine_builder=EngineBuilder(),
            runtime_service=ProductionRuntimeService(control_db=control_db),
            control_plane=repositories,
            # TODO(M2): env/file-backed SecretsPort + Redis EventBusPort replace
            # these deterministic fakes; the port seams are already final.
            secrets=FakeSecrets(),
            events=FakeEventBus(),
            mode="production",
        )

    def mock_pipeline(self) -> _MockIngestionShim:
        """Build the connector/parser pair used by the ingest smoke check."""
        return _MockIngestionShim(connector=_MockIngestionConnector(), parser=TextParser())

    def sample_pipeline(self) -> _MockIngestionShim:
        """Build a sample pipeline that ingests a small set of documents for testing and demonstration.

        TODO: Implement a sample pipeline that ingests a small set of documents for testing and demonstration.
        """
        return self.mock_pipeline()

    def diagnostics(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "runtime": self.runtime_service.diagnostics(),
            "engine": self.engine_builder.diagnostics(),
        }

    def run_mock_ingestion(self) -> dict[str, object]:
        """Run a mock ingestion pipeline that ingests a small set of documents for testing and demonstration.

        TODO: Implement a mock ingestion pipeline that ingests a small set of documents for testing and demonstration.
        """
        return self.runtime_service.run_mock_ingestion()


def _probe_control_db(dsn: str) -> dict[str, Any]:
    """Boot-time probe: ping the control DB and read the migration stamp.

    Runs its own short-lived engine + event loop and disposes it, so the
    connections never leak across loops (asyncpg pools are loop-bound).
    """
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _probe() -> dict[str, Any]:
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as connection:
                version = (
                    await connection.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                ).scalar()
            return {"ping": "ok", "migrations": version, "scheme": dsn.split(":", 1)[0]}
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001 - boot probe reports, never raises
        return {"ping": "failed", "error": str(exc), "scheme": dsn.split(":", 1)[0]}
