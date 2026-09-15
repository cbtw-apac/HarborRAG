"""Provider-neutral model/cache boundaries for semantic navigation projections."""

from typing import Protocol

from harborrag_core.security.context import AccessContext
from harborrag_core.summaries import SummaryCard
from harborrag_core.summary_cards import SummaryView


class SummaryCachePort(Protocol):
    async def get_card(self, tenant_id: str, key: str) -> SummaryCard | None: ...

    async def put_card(self, tenant_id: str, key: str, card: SummaryCard) -> SummaryCard:
        """Atomically select one immutable winner for a generation key."""
        ...


class SummaryReaderPort(Protocol):
    async def views(
        self,
        tenant_id: str,
        node_keys: tuple[str, ...],
        *,
        access: AccessContext,
        source_scopes: dict[str, str] | None = None,
    ) -> dict[str, SummaryView]: ...
