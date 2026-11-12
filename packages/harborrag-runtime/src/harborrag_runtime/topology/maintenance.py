"""Bounded topology projection audit and explicit recoverable projection cleanup."""

from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.derived import DERIVED_VECTOR_PRODUCTS, ContextualIndexProfile
from harborrag_core.topology.permissions import DerivedArtifactRecord
from harborrag_runtime.config.settings import RuntimeSettings

from .composition import connect_topology_runtime
from .embedding_profile import build_contextual_profile


async def audit(
    settings: RuntimeSettings, tenant_id: str, *, after_build_id: str | None = None
) -> dict[str, object]:
    """Inspect up to 100 eligible builds; read-only, no model calls or graph writes."""
    reports = []
    async with connect_topology_runtime(settings, provision_graph=False) as runtime:
        repo = runtime.control.topology
        ids = await repo.audit_build_ids(tenant_id, limit=100, after_build_id=after_build_id)
        context = StorageOperationContext.system(tenant_id)
        for build_id in ids:
            try:
                build = await repo.get_build(tenant_id, build_id)
                valid = build is not None and await runtime.projection.verify(
                    build, context=context
                )
                reports.append({"build_id": build_id, "verified": valid})
            except Exception as error:
                reports.append(
                    {"build_id": build_id, "verified": False, "error_code": type(error).__name__}
                )
        still_eligible = {
            build_id
            for build_id in ids
            if await repo.get_build_lineage(tenant_id, build_id) is not None
        }
        for report in reports:
            report["eligible"] = report["build_id"] in still_eligible
    return {"builds": reports, "limit": 100, "next_cursor": ids[-1] if len(ids) == 100 else None}


async def cleanup(
    settings: RuntimeSettings,
    tenant_id: str,
    *,
    apply: bool = False,
    after_build_id: str | None = None,
) -> dict[str, object]:
    """Delete rebuildable projections only after authority proves permanent retirement."""
    async with connect_topology_runtime(settings, provision_graph=False) as runtime:
        repo = runtime.control.topology
        ids = await repo.retired_build_ids(tenant_id, limit=100, after_build_id=after_build_id)
        removed = []
        removed_vector_points = 0
        current_ids = await repo.audit_build_ids(tenant_id, limit=100)
        current_derivations = {
            build_id: await repo.derivations_for_build(tenant_id, build_id)
            for build_id in current_ids
        }
        populated = {
            build_id: records for build_id, records in current_derivations.items() if records
        }
        obsolete: dict[str, tuple[DerivedArtifactRecord, ...]] = {}
        if populated:
            profile = build_contextual_profile(runtime.settings)
            obsolete = {
                build_id: selected
                for build_id, records in populated.items()
                if (selected := _obsolete_derived_records(records, profile))
            }
        if apply:
            context = StorageOperationContext.system(tenant_id)
            for build_id in ids:
                safe_builds = await repo.retired_build_ids(
                    tenant_id, build_ids=(build_id,), limit=1
                )
                if build_id not in safe_builds:
                    continue
                derivations = await repo.derivations_for_build(tenant_id, build_id)
                if derivations:
                    removed_vector_points += await runtime.cleanup_derived(
                        build_id,
                        derivations,
                        context=context,
                    )
                await runtime.projection.delete_build(build_id, context=context)
                removed.append(build_id)
            for build_id, records in obsolete.items():
                if await repo.get_build_lineage(tenant_id, build_id) is None:
                    continue
                current = await repo.derivations_for_build(tenant_id, build_id)
                artifact_ids = {record.lineage.artifact_id for record in records}
                safe_records = tuple(
                    record for record in current if record.lineage.artifact_id in artifact_ids
                )
                removed_vector_points += await runtime.cleanup_derived(
                    build_id,
                    safe_records,
                    context=context,
                )
    return {
        "candidates": ids,
        "removed_projections": removed,
        "removed_vector_points": removed_vector_points,
        "obsolete_derived_profiles": sum(len(records) for records in obsolete.values()),
        "dry_run": not apply,
        "canonical_records_and_artifacts_retained": True,
        "limit": 100,
        "next_cursor": ids[-1] if len(ids) == 100 else None,
    }


def _obsolete_derived_records(
    records: tuple[DerivedArtifactRecord, ...],
    profile: ContextualIndexProfile,
) -> tuple[DerivedArtifactRecord, ...]:
    expected = {
        product.artifact_kind: product.fingerprint(profile) for product in DERIVED_VECTOR_PRODUCTS
    }
    return tuple(
        record
        for record in records
        if record.lineage.artifact_kind in expected
        and record.lineage.metadata.get("embedding_profile")
        != expected[record.lineage.artifact_kind]
    )
