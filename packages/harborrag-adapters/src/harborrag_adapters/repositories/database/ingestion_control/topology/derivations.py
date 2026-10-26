"""Immutable post-acceptance artifacts with the accepted build's complete lineage."""

from sqlalchemy import insert, select

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.security.context import AccessContext
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.permissions import (
    BuildInputLineage,
    DerivedArtifactLineage,
    DerivedArtifactRecord,
    PermissionDependency,
)
from harborrag_core.topology.revisions import DERIVED_CAPABLE_PROJECTION_REVISIONS

from .guards import job_from_row, lock_job
from .policy_schema import BUILD_DOCUMENTS, BUILD_PERMISSIONS, DERIVED_ARTIFACTS
from .reads import eligible_builds
from .schema import TOPOLOGY_ACCEPTED, TOPOLOGY_BUILDS, TOPOLOGY_JOBS
from .transactions import topology_transaction


class TopologyDerivationOperations:
    _client: SQLAlchemyDBClient

    async def get_build_lineage(self, tenant_id: str, build_id: str) -> BuildInputLineage | None:
        """Worker-only lineage metadata; public serving always requires AccessContext."""
        async with self._client.sessions() as session:
            if (
                await session.execute(
                    eligible_builds(tenant_id).where(
                        TOPOLOGY_ACCEPTED.c.build_id == build_id,
                    )
                )
            ).first() is None:
                return None
            versions = dict(
                (
                    await session.execute(
                        select(
                            BUILD_DOCUMENTS.c.document_id,
                            BUILD_DOCUMENTS.c.document_version_id,
                        ).where(
                            BUILD_DOCUMENTS.c.tenant_id == tenant_id,
                            BUILD_DOCUMENTS.c.build_id == build_id,
                        )
                    )
                )
                .tuples()
                .all()
            )
            dependencies = (
                (
                    await session.execute(
                        select(
                            BUILD_PERMISSIONS.c.resource_kind,
                            BUILD_PERMISSIONS.c.resource_id,
                            BUILD_PERMISSIONS.c.revision,
                        ).where(
                            BUILD_PERMISSIONS.c.tenant_id == tenant_id,
                            BUILD_PERMISSIONS.c.build_id == build_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return BuildInputLineage(
            input_document_versions=versions,
            permission_dependencies=tuple(
                PermissionDependency.model_validate(dict(row)) for row in dependencies
            ),
        )

    async def eligible_artifact_ids(
        self,
        tenant_id: str,
        artifact_ids: tuple[str, ...],
        *,
        access: AccessContext | None = None,
    ) -> set[str]:
        if not artifact_ids:
            return set()
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(DERIVED_ARTIFACTS.c.artifact_id).where(
                            DERIVED_ARTIFACTS.c.tenant_id == tenant_id,
                            DERIVED_ARTIFACTS.c.artifact_id.in_(artifact_ids),
                            DERIVED_ARTIFACTS.c.build_id.in_(
                                eligible_builds(tenant_id, access=access, serving=True)
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return set(rows)

    async def publish_derivation(
        self, tenant_id: str, lineage: DerivedArtifactLineage, artifact: ArtifactReference
    ) -> None:
        async with topology_transaction(self._client) as session:
            row = (
                (
                    await session.execute(
                        select(TOPOLOGY_JOBS, TOPOLOGY_BUILDS.c.manifest.label("build_manifest"))
                        .join(
                            TOPOLOGY_BUILDS,
                            TOPOLOGY_BUILDS.c.job_id == TOPOLOGY_JOBS.c.job_id,
                        )
                        .where(
                            TOPOLOGY_BUILDS.c.tenant_id == tenant_id,
                            TOPOLOGY_BUILDS.c.build_id == lineage.build_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise HarborConflictError("derived artifact build does not exist")
            if row["build_manifest"].get(
                "projection_revision"
            ) in DERIVED_CAPABLE_PROJECTION_REVISIONS and lineage.artifact_kind in (
                "contextual_chunk",
                "parent_description",
            ):
                profile = lineage.metadata.get("embedding_profile")
                if (
                    not isinstance(profile, str)
                    or not profile
                    or lineage.artifact_id
                    != digest([lineage.build_id, lineage.artifact_kind, profile])
                ):
                    raise HarborConflictError(
                        "derived artifact requires its canonical profile-bound identity"
                    )
            await lock_job(session, job_from_row(row))
            eligible = await session.execute(
                eligible_builds(tenant_id).where(
                    TOPOLOGY_ACCEPTED.c.build_id == lineage.build_id,
                )
            )
            if eligible.first() is None:
                raise HarborConflictError("derived artifact build is no longer accepted")
            versions = dict(
                (
                    await session.execute(
                        select(
                            BUILD_DOCUMENTS.c.document_id,
                            BUILD_DOCUMENTS.c.document_version_id,
                        ).where(
                            BUILD_DOCUMENTS.c.tenant_id == tenant_id,
                            BUILD_DOCUMENTS.c.build_id == lineage.build_id,
                        )
                    )
                )
                .tuples()
                .all()
            )
            dependencies = (
                (
                    await session.execute(
                        select(
                            BUILD_PERMISSIONS.c.resource_kind,
                            BUILD_PERMISSIONS.c.resource_id,
                            BUILD_PERMISSIONS.c.revision,
                        ).where(
                            BUILD_PERMISSIONS.c.tenant_id == tenant_id,
                            BUILD_PERMISSIONS.c.build_id == lineage.build_id,
                        )
                    )
                )
                .tuples()
                .all()
            )
            supplied = {
                (item.resource_kind, item.resource_id, item.revision)
                for item in lineage.permission_dependencies
            }
            if versions != lineage.input_document_versions or set(dependencies) != supplied:
                raise HarborConflictError(
                    "derived artifact must preserve all accepted input proofs"
                )
            values = {
                "tenant_id": tenant_id,
                "artifact_id": lineage.artifact_id,
                "build_id": lineage.build_id,
                "artifact_kind": lineage.artifact_kind,
                "lineage": lineage.model_dump(mode="json"),
                "artifact": artifact.model_dump(mode="json"),
            }
            existing = (
                (
                    await session.execute(
                        select(DERIVED_ARTIFACTS).where(
                            DERIVED_ARTIFACTS.c.tenant_id == tenant_id,
                            DERIVED_ARTIFACTS.c.artifact_id == lineage.artifact_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if dict(existing) != values:
                    raise HarborConflictError("derived artifact is immutable")
                return
            await session.execute(insert(DERIVED_ARTIFACTS).values(**values))

    async def active_derivations(
        self,
        tenant_id: str,
        *,
        access: AccessContext | None = None,
        artifact_kind: str | None = None,
        limit: int = 100,
    ) -> tuple[DerivedArtifactRecord, ...]:
        eligible = eligible_builds(tenant_id, access=access, serving=True)
        query = select(DERIVED_ARTIFACTS).where(
            DERIVED_ARTIFACTS.c.tenant_id == tenant_id,
            DERIVED_ARTIFACTS.c.build_id.in_(eligible),
        )
        if artifact_kind is not None:
            query = query.where(DERIVED_ARTIFACTS.c.artifact_kind == artifact_kind)
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        query.order_by(DERIVED_ARTIFACTS.c.artifact_id).limit(
                            max(1, min(limit, 10000)) + 1
                        )
                    )
                )
                .mappings()
                .all()
            )
        if len(rows) > max(1, min(limit, 10000)):
            raise HarborConflictError("derived artifact enumeration exceeds bounded limit")
        return tuple(
            DerivedArtifactRecord(lineage=row["lineage"], artifact=row["artifact"]) for row in rows
        )

    async def derivations_for_build(
        self,
        tenant_id: str,
        build_id: str,
        *,
        limit: int = 100,
    ) -> tuple[DerivedArtifactRecord, ...]:
        """Read frozen products for one build, including permanently retired builds.

        This authority-only view exists for projection cleanup. Serving must use
        ``active_derivations`` so a retired or permission-stale build stays hidden.
        """

        bounded_limit = max(1, min(limit, 1000))
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(DERIVED_ARTIFACTS)
                        .where(
                            DERIVED_ARTIFACTS.c.tenant_id == tenant_id,
                            DERIVED_ARTIFACTS.c.build_id == build_id,
                        )
                        .order_by(DERIVED_ARTIFACTS.c.artifact_id)
                        .limit(bounded_limit + 1)
                    )
                )
                .mappings()
                .all()
            )
        if len(rows) > bounded_limit:
            raise HarborConflictError("derived build enumeration exceeds bounded limit")
        return tuple(
            DerivedArtifactRecord(lineage=row["lineage"], artifact=row["artifact"]) for row in rows
        )
