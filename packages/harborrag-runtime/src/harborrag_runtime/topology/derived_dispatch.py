"""Bounded worker discovery for missing products, independent of extraction leases."""

import asyncio
import logging

from harborrag_core.ports.topology import TopologyRepositoryPort
from harborrag_runtime.config.settings import RuntimeSettings

from .embedding_profile import build_contextual_profile
from .service import DerivationRunner

logger = logging.getLogger("harborrag.runtime.topology")


class DerivedDispatcher:
    def __init__(
        self,
        repository: TopologyRepositoryPort,
        runner: DerivationRunner,
        settings: RuntimeSettings,
    ) -> None:
        self._repository, self._runner, self._settings = repository, runner, settings
        self._cursor: str | None = None

    async def run_page(self, tenant_id: str, *, limit: int = 10) -> int:
        if not self._settings.topology_derived_enabled:
            return 0
        profile = build_contextual_profile(self._settings)
        builds = await self._repository.pending_derivation_build_ids(
            tenant_id,
            profile.fingerprint,
            parent_profile=profile.parent_fingerprint,
            limit=limit,
            after_build_id=self._cursor,
        )
        for build_id in builds:
            try:
                async with asyncio.timeout(self._settings.topology_job_seconds):
                    states = await self._runner(tenant_id, build_id)
                logger.info(
                    "Derived enrichment progress", extra={"build_id": build_id, "readiness": states}
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Derived enrichment remains pending",
                    extra={"build_id": build_id, "error_code": type(error).__name__},
                )
        self._cursor = builds[-1] if len(builds) == limit else None
        return len(builds)

    async def watch(self, tenant_id: str) -> None:
        while True:
            try:
                await self.run_page(tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Derived dispatcher unavailable", extra={"error_code": type(error).__name__}
                )
            await asyncio.sleep(self._settings.topology_poll_seconds)
