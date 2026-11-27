"""Bounded worker discovery for missing products, independent of extraction leases."""

import asyncio
import logging

from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_core.ports.topology import TopologyDerivationRepositoryPort
from harborrag_runtime.config.settings import RuntimeSettings

from .embedding_profile import build_contextual_profile
from .service import DerivationRunner

logger = logging.getLogger("harborrag.runtime.topology")

DEFAULT_PAGE_LIMIT = 10
"""Builds one dispatch page runs when the caller sets no limit."""


class DerivedDispatcher:
    """Find accepted builds still missing derived products and run them a page at a time.

    A product that fails stays pending and is retried on a later page. The
    cursor only advances past a full page, so a short page starts over.

    Args:
        repository: Reads which builds still lack the current profile's products.
        runner: Runs the derived products of one build.
        settings: Runtime settings holding the switch, timeouts and poll interval.
    """

    def __init__(
        self,
        repository: TopologyDerivationRepositoryPort,
        runner: DerivationRunner,
        settings: RuntimeSettings,
    ) -> None:
        self._repository, self._runner, self._settings = repository, runner, settings
        self._cursor: str | None = None

    async def run_page(self, tenant_id: str, *, limit: int = DEFAULT_PAGE_LIMIT) -> int:
        """Run the derived products of one page of pending builds.

        A build that fails or times out is logged and left pending.

        Args:
            tenant_id: Tenant whose builds are dispatched.
            limit: Most builds run in this page.

        Returns:
            The number of builds the page held, 0 when derived products are off.
        """
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
                log_event(
                    logger,
                    "topology.derived_enrichment_progress",
                    tenant_id=tenant_id,
                    build_id=build_id,
                    readiness_status=_readiness_status(states),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_failure(
                    logger,
                    "topology.derived_enrichment_pending",
                    error,
                    FailureKind.EXPECTED,
                    tenant_id=tenant_id,
                    build_id=build_id,
                )
        self._cursor = builds[-1] if len(builds) == limit else None
        return len(builds)

    async def watch(self, tenant_id: str) -> None:
        """Run pages forever, one poll interval apart, until cancelled.

        A page that fails is logged and the next poll tries again.

        Args:
            tenant_id: Tenant whose builds are dispatched.
        """
        while True:
            try:
                await self.run_page(tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_failure(
                    logger,
                    "topology.derived_dispatcher_unavailable",
                    error,
                    FailureKind.EXPECTED,
                    tenant_id=tenant_id,
                )
            await asyncio.sleep(self._settings.topology_poll_seconds)


def _readiness_status(states: dict[str, str]) -> str:
    """Join each product's state word, such as ``contextual:ready,parents:pending``.

    A state can carry a detail after a colon, such as a deferral reason, which is
    left out.
    """
    return ",".join(
        f"{product}:{state.partition(':')[0]}" for product, state in sorted(states.items())
    )
