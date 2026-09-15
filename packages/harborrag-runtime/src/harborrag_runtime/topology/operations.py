"""Operator entry points for opt-in, backfill, status and projection recovery."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.topology import default_extraction_profile, pinned_configuration
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security.context import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import ExtractionProfile, TopologyPolicy, digest
from harborrag_core.topology.records import TopologyBuildContent
from harborrag_core.topology.resolution import ResolutionRequest
from harborrag_engine.topology.assembly import build_identity
from harborrag_runtime.config.graph_build import GraphBuildConfig, graph_build_runtime_view
from harborrag_runtime.config.settings import RuntimeSettings

from .composition import connect_topology_authority, connect_topology_runtime
from .configuration import GraphBuildConfigSynchronizer, GraphBuildProfileFactory
from .derived_dispatch import DerivedDispatcher
from .inspection import TopologyInspectionBuilder


async def configure(
    settings: RuntimeSettings,
    policy: TopologyPolicy,
) -> dict[str, object]:
    """Changing source policy creates topology jobs, never new document versions."""
    if policy.enabled:
        pinned_configuration(
            HarborChatClientConfig.from_file(settings.model_config_path), policy.profile
        )
    async with connect_topology_authority(settings) as control:
        revision = await control.topology.configure_policy(policy)
        count = await control.topology.reconcile(policy.tenant_id)
        stored = await control.topology.get_policy(policy.tenant_id, policy.source_scope_id)
        return {
            "policy_revision": revision,
            "enqueued": count,
            "enabled": policy.enabled,
            "fingerprint": stored.fingerprint if stored else policy.fingerprint,
        }


def make_profile(settings: RuntimeSettings, model: str | None = None) -> ExtractionProfile:
    return default_extraction_profile(
        HarborChatClientConfig.from_file(settings.model_config_path), model
    )


async def disable(settings: RuntimeSettings, tenant_id: str, scope: str) -> dict[str, object]:
    async with connect_topology_authority(settings) as control:
        policy = await control.topology.get_policy(tenant_id, scope)
        if policy is None:
            raise ValueError("source has no topology policy")
        revision = await control.topology.configure_policy(
            policy.model_copy(update={"enabled": False})
        )
        return {"enabled": False, "policy_revision": revision}


async def status(settings: RuntimeSettings, tenant_id: str) -> list[dict[str, object]]:
    async with connect_topology_authority(settings) as control:
        jobs = await control.topology.list_jobs(tenant_id)
        return [
            {
                "job_id": job.job_id,
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "state": job.state,
                "attempts": job.attempts,
                "error_code": job.error_code,
                "available_at": job.available_at,
                "policy_revision": job.policy_revision,
                "build_id": build_identity(job) if job.state == "accepted" else None,
            }
            for job in jobs
        ]


async def run(
    settings: RuntimeSettings, tenant_id: str, *, limit: int = 100, watch: bool = False
) -> list[dict[str, object]]:
    """DIRECT durable worker; process limits count attempts, including failed work."""
    reports: list[dict[str, object]] = []
    async with connect_topology_runtime(settings) as runtime:
        settings = runtime.settings
        derived = DerivedDispatcher(runtime.control.topology, runtime.derive, settings)
        while True:
            await runtime.control.topology.reconcile(tenant_id)
            for _ in range(limit):
                result = await runtime.service.run_once(tenant_id)
                if result.state == "idle":
                    break
                if not watch:
                    reports.append(asdict(result))
            if not watch:
                return reports
            await derived.run_page(tenant_id)
            await asyncio.sleep(settings.topology_poll_seconds)


async def apply_graph_build_config(settings: RuntimeSettings) -> dict[str, object]:
    """Validate and synchronize the desired graph-build policy explicitly."""

    config = GraphBuildConfig.from_settings(settings)
    effective = config.effective_settings(settings)
    async with connect_topology_authority(effective) as control:
        report = await GraphBuildConfigSynchronizer(
            config,
            control.topology,
            GraphBuildProfileFactory(effective.model_config_path),
        ).apply()
    return {
        "config_path": str(Path(settings.graph_build_config_path).expanduser().resolve()),
        "runtime": graph_build_runtime_view(effective),
        "synchronization": report.as_dict(),
    }


async def rebuild(settings: RuntimeSettings, tenant_id: str, build_id: str) -> dict[str, object]:
    """Restore only an eligible generation from frozen data, with no model calls."""
    async with connect_topology_runtime(settings) as runtime:
        repo = runtime.control.topology
        if await repo.get_build_lineage(tenant_id, build_id) is None:
            raise ValueError("topology build is not currently eligible")
        build = await repo.get_build(tenant_id, build_id)
        if build is None:
            raise ValueError("topology build is missing")
        context = StorageOperationContext.system(tenant_id)
        payload = await runtime.artifact_reader.get(build.artifact, context=context)
        if sha256(payload).hexdigest() != build.artifact.sha256:
            raise ValueError("topology build artifact checksum mismatch")
        frozen = TopologyBuildContent.model_validate_json(payload)
        canonical = TopologyBuildContent.model_validate(build.model_dump(exclude={"artifact"}))
        if _topology_content_digest(frozen) != _topology_content_digest(canonical):
            raise ValueError("canonical topology records do not match frozen artifact")
        await runtime.projection.write(build, context=context)
        valid = await runtime.projection.verify(build, context=context)
        eligible = await repo.get_build_lineage(tenant_id, build_id) is not None
        return {"build_id": build_id, "verified": valid, "eligible": eligible}


def _topology_content_digest(content: TopologyBuildContent) -> str:
    """Compare set-like records independently of canonical database read order."""
    payload = content.model_dump(mode="json")
    payload["mentions"] = sorted(payload["mentions"], key=lambda item: item["mention_id"])
    payload["assertions"] = sorted(payload["assertions"], key=lambda item: item["assertion_id"])
    return digest(payload)


async def entities(
    settings: RuntimeSettings,
    tenant_id: str,
    label: str | None = None,
    *,
    principal_id: str | None = None,
) -> list[dict[str, object]]:
    async with connect_topology_authority(settings) as control:
        mentions = await control.topology.active_mentions(
            tenant_id,
            labels=(label,) if label else (),
            limit=100,
            access=AccessContext(principal_id=principal_id, tenant_id=TenantId(tenant_id))
            if principal_id
            else None,
        )
        return [
            {
                "entity_id": item.entity_id,
                "label": item.observation.name,
                "type": item.observation.entity_type,
                "document_id": item.document_id,
                "chunk_id": item.chunk_id,
                "build_id": item.build_id,
            }
            for item in mentions
        ]


async def inspect_build(
    settings: RuntimeSettings,
    tenant_id: str,
    build_id: str,
    *,
    principal_id: str,
    limit: int = 20,
) -> dict[str, object]:
    """Return generated topology and exact evidence only after canonical ACL checks."""
    access = AccessContext(principal_id=principal_id, tenant_id=TenantId(tenant_id))
    async with connect_topology_authority(settings) as control:
        repo = control.topology
        eligible = await repo.eligible_build_ids(tenant_id, (build_id,), access=access)
        if build_id not in eligible:
            # One error covers missing, retired, stale-ACL, and forbidden builds.
            raise ValueError("topology build is unavailable")
        topology = await repo.get_build(tenant_id, build_id)
        if topology is None:
            raise ValueError("topology build is unavailable")
        job = await repo.get_job(tenant_id, topology.job_id)
        if job is None:
            raise ValueError("topology build is unavailable")
        checkpoints = await repo.checkpoints(job)
        deployment = _resolved_deployment(settings, job.policy.profile)
        result = TopologyInspectionBuilder(topology, job, checkpoints, deployment).build(
            limit=limit
        )
        if build_id not in await repo.eligible_build_ids(tenant_id, (build_id,), access=access):
            raise ValueError("topology build is unavailable")
        return result


def _resolved_deployment(
    settings: RuntimeSettings, profile: ExtractionProfile
) -> dict[str, object]:
    """Name a deployment only when current config matches the immutable revision."""
    model_config_path = getattr(settings, "model_config_path", None)
    if model_config_path is None:
        return {"configuration_match": False}
    try:
        config = pinned_configuration(HarborChatClientConfig.from_file(model_config_path), profile)
        _, logical = config.model_for(profile.model)
        deployment = logical.deployments[0]
    except (KeyError, OSError, ValueError):
        return {"configuration_match": False}
    return {
        "configuration_match": True,
        "deployment_name": deployment.name,
        "provider": deployment.provider.value,
        "provider_model": deployment.model,
    }


async def derive(settings: RuntimeSettings, tenant_id: str, build_id: str) -> object:
    """Generate independent contextual/parent artifacts for an accepted build."""
    async with connect_topology_runtime(settings) as runtime:
        return await runtime.derive(tenant_id, build_id)


async def resolve(settings: RuntimeSettings, request: ResolutionRequest) -> dict[str, object]:
    async with connect_topology_authority(settings) as control:
        decision = await control.topology.record_resolution(request)
        count = await control.topology.reconcile(request.tenant_id)
        return {"decision": decision.model_dump(mode="json"), "enqueued": count}


async def resolutions(settings: RuntimeSettings, tenant_id: str) -> list[dict[str, object]]:
    async with connect_topology_authority(settings) as control:
        return [
            item.model_dump(mode="json")
            for item in await control.topology.list_resolutions(tenant_id)
        ]


async def evaluate(payload: str) -> dict[str, object]:
    """Offline, bounded, exact extraction evaluation; never contacts a model."""
    from harborrag_core.topology.evaluation import TopologyEvaluationUnit
    from harborrag_engine.topology.evaluation import evaluate_extractions

    if len(payload) > 20_000_000:
        raise ValueError("evaluation input exceeds 20 million characters")
    rows = [line for line in payload.splitlines() if line.strip()]
    if not rows or len(rows) > 10000:
        raise ValueError("evaluation requires between 1 and 10000 units")
    units = tuple(TopologyEvaluationUnit.model_validate_json(line) for line in rows)
    return evaluate_extractions(units).model_dump(mode="json")
