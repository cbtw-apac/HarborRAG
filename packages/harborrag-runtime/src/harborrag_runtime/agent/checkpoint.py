"""Agent-run checkpoint contracts and runtime implementations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ports.agent_runs import AgentCheckpoint, AgentRunIdentity, AgentRunRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings


class InMemoryAgentRunRepository:
    """Bounded process-local implementation for unit tests and local checks."""

    def __init__(self, *, max_runs: int = 10_000) -> None:
        if max_runs < 1:
            raise ValueError("agent run repository bounds must be positive")
        self._max_runs = max_runs
        self._checkpoints: OrderedDict[str, AgentCheckpoint] = OrderedDict()
        self._lock = asyncio.Lock()

    async def create(self, checkpoint: AgentCheckpoint) -> None:
        async with self._lock:
            run_id = checkpoint.identity.run_id
            self._checkpoints[run_id] = checkpoint
            self._checkpoints.move_to_end(run_id)
            while len(self._checkpoints) > self._max_runs:
                self._checkpoints.popitem(last=False)

    async def save_step(self, checkpoint: AgentCheckpoint) -> None:
        async with self._lock:
            run_id = checkpoint.identity.run_id
            current = self._checkpoints.get(run_id)
            if current is None or current.version != checkpoint.version - 1:
                raise HarborConflictError(f"agent run {run_id!r} checkpoint version conflict")
            self._checkpoints[run_id] = checkpoint
            self._checkpoints.move_to_end(run_id)

    async def get(self, identity: AgentRunIdentity) -> AgentCheckpoint | None:
        async with self._lock:
            checkpoint = self._checkpoints.get(identity.run_id)
            if checkpoint is None:
                return None
            owner = checkpoint.identity
            if (owner.tenant_id, owner.principal_id, owner.session_id) != (
                identity.tenant_id,
                identity.principal_id,
                identity.session_id,
            ):
                return None
            return checkpoint


@dataclass(slots=True)
class DatabaseAgentRunRepository:
    """Runtime-owned SQL checkpoint plugin; production DSNs use PostgreSQL/asyncpg."""

    repository: AgentRunRepository
    engine: AsyncEngine

    @classmethod
    def configured(
        cls,
        settings: RuntimeSettings | None = None,
    ) -> DatabaseAgentRunRepository:
        """Migrate and open the configured control DB for a standalone process."""

        from harborrag_adapters.repositories.database.control_plane.agent_runs import (
            SqlAgentRunRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
            create_session_factory,
        )
        from harborrag_adapters.repositories.database.control_plane.migrations import (
            run_migrations,
        )
        from harborrag_core.contracts.errors import HarborConfigurationError
        from harborrag_runtime.config.settings import DEFAULT_CONTROL_DB_URL, RuntimeSettings

        selected = settings or RuntimeSettings()
        dsn = selected.control_db_url.get_secret_value()
        if selected.env == "prod" and dsn == DEFAULT_CONTROL_DB_URL:
            raise HarborConfigurationError(
                "agent run checkpoints require HARBORRAG_CONTROL_DB_URL in production"
            )
        run_migrations(dsn)
        engine = create_control_plane_engine(dsn)
        repository = SqlAgentRunRepository(create_session_factory(engine))
        return cls(repository=repository, engine=engine)

    async def create(self, checkpoint: AgentCheckpoint) -> None:
        await self.repository.create(checkpoint)

    async def save_step(self, checkpoint: AgentCheckpoint) -> None:
        await self.repository.save_step(checkpoint)

    async def get(self, identity: AgentRunIdentity) -> AgentCheckpoint | None:
        return await self.repository.get(identity)

    async def aclose(self) -> None:
        await self.engine.dispose()


__all__ = [
    "DatabaseAgentRunRepository",
    "InMemoryAgentRunRepository",
]
