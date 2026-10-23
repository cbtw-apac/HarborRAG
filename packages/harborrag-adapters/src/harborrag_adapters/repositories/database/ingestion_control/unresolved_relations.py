"""Source links whose targets no ingested scope has published yet."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Exists, Select, and_, delete, exists, insert, or_, select
from sqlalchemy.sql.selectable import NamedFromClause

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
            # Serialize reconciliations of one declarer. Two runs can repair it at once
            # (one's reverse pass, another's forward repair), and under READ COMMITTED
            # the second DELETE cannot see rows the first is inserting, so its INSERT
            # then collides with them on the primary key.
            await session.execute(_declarer_lock(declaring_document_id))
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
        the declaring connection, a cross-connector target in exactly one connection.
        Repair declines a cross-connector target active in several connections, so
        offering its declarer would re-repair it on every run, to the same unresolved
        row, and spend the caller's budget before later declarers are reached. The
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
                    and_(
                        unresolved.c.target_connector_type == unresolved.c.connector_type,
                        target.c.connection_id == unresolved.c.connection_id,
                    ),
                    and_(
                        unresolved.c.target_connector_type != unresolved.c.connector_type,
                        ~_active_in_another_connection(target),
                    ),
                ),
            )
            .distinct()
            .order_by(unresolved.c.declaring_document_id)
            .limit(limit)
        )
        async with self._client.sessions() as session:
            result = await session.execute(statement)
            return tuple(result.scalars().all())


def _declarer_lock(declaring_document_id: str) -> Select[tuple[str]]:
    """Lock the declarer's document row for the rest of the transaction.

    NO KEY UPDATE (``key_share``) is enough to serialize two reconciliations and still
    lets other tables' foreign-key checks against the row proceed. SQLite, which
    serializes writers anyway, renders no lock clause at all.
    """

    return (
        select(DOCUMENTS.c.document_id)
        .where(DOCUMENTS.c.document_id == declaring_document_id)
        .with_for_update(key_share=True)
    )


def _active_in_another_connection(target: NamedFromClause) -> Exists:
    """Whether the target's source item is also active under a different connection.

    ``resolve_unambiguous_active_sources`` resolves a cross-connector target only when
    exactly one connection holds it. ``uq_document_source_identity`` makes a connection
    hold an item at most once, so one more active connection is what makes it ambiguous;
    the same document's extra ``source_items`` rows (one per scope) are not.
    """

    other = DOCUMENTS.alias("unresolved_other_target")
    other_item = SOURCE_ITEMS.alias("unresolved_other_target_item")
    return exists(
        select(other.c.document_id)
        .join(
            other_item,
            and_(
                other_item.c.document_id == other.c.document_id,
                other_item.c.is_active.is_(True),
            ),
        )
        .where(
            other.c.tenant_id == target.c.tenant_id,
            other.c.connector_type == target.c.connector_type,
            other.c.source_item_id == target.c.source_item_id,
            other.c.connection_id != target.c.connection_id,
            other.c.active_document_version_id.is_not(None),
        )
    )


__all__ = ["UnresolvedRelationRepository", "UnresolvedSourceRelation"]
