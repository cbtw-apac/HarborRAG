"""Summary projection: cache operations."""

from sqlalchemy import select

from harborrag_core.base import utc_now
from harborrag_core.summaries import (
    SummaryCard,
)

from .summary_authority import SummaryAuthority
from .summary_intent import upsert
from .summary_schema import SUMMARY_CACHE
from .topology.transactions import topology_transaction


class SummaryCacheOperations(SummaryAuthority):
    async def get_card(self, tenant_id: str, key: str) -> SummaryCard | None:
        async with self._client.sessions() as session:
            row = (
                (
                    await session.execute(
                        select(SUMMARY_CACHE).where(
                            SUMMARY_CACHE.c.tenant_id == tenant_id,
                            SUMMARY_CACHE.c.generation_key == key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        card = SummaryCard.model_validate(row["card"])
        if card.artifact_hash != row["artifact_hash"]:
            raise ValueError("summary cache integrity failure")
        return card

    async def put_card(self, tenant_id: str, key: str, card: SummaryCard) -> SummaryCard:
        async with topology_transaction(self._client) as session:
            await session.execute(
                upsert(session)(SUMMARY_CACHE)
                .values(
                    tenant_id=tenant_id,
                    generation_key=key,
                    artifact_hash=card.artifact_hash,
                    card=card.model_dump(mode="json"),
                    created_at=utc_now(),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "generation_key"])
            )
        winner = await self.get_card(tenant_id, key)
        assert winner is not None
        return winner
