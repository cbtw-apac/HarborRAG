"""Worker-only durable post-acceptance stage discovery, filtered before pagination."""

from sqlalchemy import exists, func, or_, select

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient

from .policy_schema import DERIVED_ARTIFACTS
from .reads import eligible_builds
from .schema import TOPOLOGY_BUILDS


class TopologyReadinessOperations:
    _client: SQLAlchemyDBClient

    async def pending_derivation_build_ids(
        self,
        tenant_id: str,
        embedding_profile: str,
        *,
        parent_profile: str | None = None,
        limit: int = 100,
        after_build_id: str | None = None,
    ) -> tuple[str, ...]:
        if not embedding_profile or parent_profile == "":
            raise ValueError("derived readiness requires an embedding profile")
        stages = tuple(
            exists(
                select(DERIVED_ARTIFACTS.c.artifact_id).where(
                    DERIVED_ARTIFACTS.c.tenant_id == tenant_id,
                    DERIVED_ARTIFACTS.c.build_id == TOPOLOGY_BUILDS.c.build_id,
                    DERIVED_ARTIFACTS.c.artifact_kind == kind,
                    DERIVED_ARTIFACTS.c.lineage["metadata"]["embedding_profile"].as_string()
                    == profile,
                )
            )
            for kind, profile in (
                ("contextual_chunk", embedding_profile),
                ("parent_description", parent_profile or embedding_profile),
            )
        )
        query = select(TOPOLOGY_BUILDS.c.build_id).where(
            TOPOLOGY_BUILDS.c.tenant_id == tenant_id,
            TOPOLOGY_BUILDS.c.build_id.in_(eligible_builds(tenant_id)),
            TOPOLOGY_BUILDS.c.manifest["projection_revision"].as_string().in_(
                ("semantic-v2", "semantic-v3", "semantic-v4", "semantic-v5", "semantic-v6")
            ),
            func.json_array_length(TOPOLOGY_BUILDS.c.manifest["chunk_ids"]) > 0,
            or_(~stages[0], ~stages[1]),
        )
        if after_build_id is not None:
            query = query.where(TOPOLOGY_BUILDS.c.build_id > after_build_id)
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        query.order_by(TOPOLOGY_BUILDS.c.build_id).limit(max(1, min(limit, 1000)))
                    )
                )
                .scalars()
                .all()
            )
        return tuple(rows)
