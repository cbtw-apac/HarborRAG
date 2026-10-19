"""Source links whose targets no ingested scope has published yet."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, delete, insert, or_, select

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now

from .schema import DOCUMENTS, SOURCE_ITEMS, UNRESOLVED_SOURCE_RELATIONS


@dataclass(frozen=True, slots=True)
class UnresolvedSourceRelation:
    """One declared link whose target relation repair could not resolve."""

    target_source_item_id: str
    target_connector_type: str
    predicate: str
    relation_type: str


class UnresolvedRelationRepository:
    """Remember unresolved links so a later run can re-repair their declarers."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def replace_unresolved(  # noqa: PLR0913
        self,
        *,
        tenant_id: str,
        declaring_document_id: str,
        declaring_document_version_id: str,
        connector_type: str,
        connection_id: str,
        relations: Sequence[UnresolvedSourceRelation],
    ) -> None:
        """Reconcile one document's whole unresolved set, an empty set included."""

        rows = {
            (relation.target_source_item_id, relation.predicate): relation for relation in relations
        }
        now = utc_now()
        async with self._client.sessions.begin() as session:
            await session.execute(
                delete(UNRESOLVED_SOURCE_RELATIONS).where(
                    UNRESOLVED_SOURCE_RELATIONS.c.declaring_document_id == declaring_document_id
                )
            )
            if rows:
                await session.execute(
                    insert(UNRESOLVED_SOURCE_RELATIONS),
                    [
                        {
                            "declaring_document_id": declaring_document_id,
                            "target_source_item_id": relation.target_source_item_id,
                            "predicate": relation.predicate,
                            "tenant_id": tenant_id,
                            "declaring_document_version_id": declaring_document_version_id,
                            "connector_type": connector_type,
                            "connection_id": connection_id,
                            "target_connector_type": relation.target_connector_type,
                            "relation_type": relation.relation_type,
                            "updated_at": now,
                        }
                        for relation in rows.values()
                    ],
                )

    async def unresolved_for(
        self, declaring_document_id: str
    ) -> tuple[UnresolvedSourceRelation, ...]:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(UNRESOLVED_SOURCE_RELATIONS)
                .where(UNRESOLVED_SOURCE_RELATIONS.c.declaring_document_id == declaring_document_id)
                .order_by(
                    UNRESOLVED_SOURCE_RELATIONS.c.target_source_item_id,
                    UNRESOLVED_SOURCE_RELATIONS.c.predicate,
                )
            )
            return tuple(
                UnresolvedSourceRelation(
                    target_source_item_id=row["target_source_item_id"],
                    target_connector_type=row["target_connector_type"],
                    predicate=row["predicate"],
                    relation_type=row["relation_type"],
                )
                for row in result.mappings().all()
            )

    async def resolvable_declaring_documents(
        self,
        *,
        tenant_id: str,
        after: str = "",
        limit: int = 500,
    ) -> tuple[str, ...]:
        """Declaring documents with a link whose target has since been published.

        Mirrors relation repair's own resolution: a same-connector target must be in
        the declaring connection, a cross-connector target in any connection (repair
        still declines an ambiguous one, which the caller's cursor tolerates). The
        declaring document must itself be active, or repair has nothing to rebuild.
        """

        if not 1 <= limit <= 10_000:
            raise ValueError("resolvable declarer limit must be between 1 and 10000")
        unresolved = UNRESOLVED_SOURCE_RELATIONS
        target = DOCUMENTS.alias("unresolved_target")
        declaring = DOCUMENTS.alias("unresolved_declarer")
        statement = (
            select(unresolved.c.declaring_document_id)
            .join(
                target,
                and_(
                    target.c.tenant_id == unresolved.c.tenant_id,
                    target.c.connector_type == unresolved.c.target_connector_type,
                    target.c.source_item_id == unresolved.c.target_source_item_id,
                    target.c.active_document_version_id.is_not(None),
                ),
            )
            .join(
                SOURCE_ITEMS,
                and_(
                    SOURCE_ITEMS.c.document_id == target.c.document_id,
                    SOURCE_ITEMS.c.is_active.is_(True),
                ),
            )
            .join(declaring, declaring.c.document_id == unresolved.c.declaring_document_id)
            .where(
                unresolved.c.tenant_id == tenant_id,
                unresolved.c.declaring_document_id > after,
                declaring.c.active_document_version_id.is_not(None),
                or_(
                    unresolved.c.target_connector_type != unresolved.c.connector_type,
                    target.c.connection_id == unresolved.c.connection_id,
                ),
            )
            .distinct()
            .order_by(unresolved.c.declaring_document_id)
            .limit(limit)
        )
        async with self._client.sessions() as session:
            result = await session.execute(statement)
            return tuple(result.scalars().all())


__all__ = ["UnresolvedRelationRepository", "UnresolvedSourceRelation"]
