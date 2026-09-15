"""Durable, fenced enrichment runner shared by DIRECT and Temporal execution."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Protocol

from harborrag_adapters.repositories.object_store import ChunkArtifactReader
from harborrag_adapters.topology.artifacts import ExtractionArtifacts
from harborrag_core.chunking import ChunkRecord, RecordKind
from harborrag_core.chunking.identity import encoded_identifier
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import DocumentVersionSnapshot
from harborrag_core.ports.topology import TopologyRepositoryPort
from harborrag_core.ports.topology_extraction import EntityExtractionPort
from harborrag_core.ports.topology_projection import TopologyProjectionPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import (
    ChunkExtractionCheckpoint,
    ChunkExtractionInput,
    DocumentTopologyBuild,
    ExtractionOutput,
    ExtractionProfile,
    TopologyJob,
)
from harborrag_engine.topology.assembly import assemble_observations, build_identity
from harborrag_engine.topology.build_builder import EnrichmentBuildBuilder

from .budgeted_extractor import BudgetedExtractor, EnrichmentDeferredError
from .chunk_runner import ChunkExtractionRunner

logger = logging.getLogger("harborrag.runtime.topology")
type ExtractorFactory = Callable[
    [ExtractionProfile], AbstractAsyncContextManager[EntityExtractionPort]
]
type DerivationRunner = Callable[[str, str], Awaitable[dict[str, str]]]


class VersionReader(Protocol):
    async def get_version(self, document_version_id: str) -> DocumentVersionSnapshot | None: ...


@dataclass(frozen=True)
class TopologyRunResult:
    job_id: str | None
    state: str
    build_id: str | None = None
    extracted_chunks: int = 0
    reused_chunks: int = 0
    error_code: str | None = None
    derived: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TopologyResources:
    repository: TopologyRepositoryPort
    versions: VersionReader
    chunks: ChunkArtifactReader
    artifacts: ExtractionArtifacts
    projection: TopologyProjectionPort
    extractor: ExtractorFactory
    llm_operation_cost_usd: Decimal | None = None
    require_budget: bool = False
    derive: DerivationRunner | None = None


class TopologyEnrichmentService:
    def __init__(
        self,
        resources: TopologyResources,
        *,
        lease_seconds: int = 300,
        job_seconds: float = 3600,
        max_chunks: int = 1000,
    ) -> None:
        if lease_seconds < 3 or job_seconds <= 0 or max_chunks < 1:
            raise ValueError("topology runner budgets must be positive")
        self._resources = resources
        self._lease_seconds = lease_seconds
        self._job_seconds = job_seconds
        self._max_chunks = max_chunks

    async def run_once(
        self,
        tenant_id: str,
        *,
        job_id: str | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> TopologyRunResult:
        repository = self._resources.repository
        job = await repository.claim(tenant_id, lease_seconds=self._lease_seconds, job_id=job_id)
        if job is None:
            current = await repository.get_job(tenant_id, job_id) if job_id else None
            return TopologyRunResult(job_id, current.state if current else "idle")
        try:
            async with asyncio.timeout(self._job_seconds):
                done = asyncio.Event()
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._renew(job, done, heartbeat))
                    try:
                        result = await self._process(job)
                    finally:
                        done.set()
            logger.info(
                "Topology build completed",
                extra={
                    "job_id": job.job_id,
                    "tenant_id": tenant_id,
                    "build_id": result.build_id,
                    "extracted_chunks": result.extracted_chunks,
                    "reused_chunks": result.reused_chunks,
                },
            )
            return await self._derive(tenant_id, result)
        except asyncio.CancelledError:
            await asyncio.shield(self._fail(job, "cancelled"))
            raise
        except Exception as error:
            if deferred := _deferred_error(error):
                try:
                    await repository.defer_job(
                        job,
                        str(deferred),
                        retry_after=deferred.retry_after,
                    )
                except HarborConflictError:
                    pass
                return TopologyRunResult(job.job_id, "deferred", error_code=str(deferred))
            code = _failure_code(error)
            await self._fail(job, code)
            logger.warning(
                "Topology attempt failed",
                exc_info=True,
                extra={
                    "job_id": job.job_id,
                    "tenant_id": tenant_id,
                    "error_code": code,
                },
            )
            return TopologyRunResult(job.job_id, "failed", error_code=code)

    async def _derive(self, tenant_id: str, result: TopologyRunResult) -> TopologyRunResult:
        runner = self._resources.derive
        if runner is None or result.state != "accepted" or result.build_id is None:
            return result
        try:
            async with asyncio.timeout(self._job_seconds):
                states = await runner(tenant_id, result.build_id)
        except Exception as error:
            states = {"contextual": f"pending:{type(error).__name__}", "parents": "pending"}
        return replace(result, derived=states)

    async def _fail(self, job: TopologyJob, code: str) -> None:
        try:
            await self._resources.repository.fail(job, code)
        except HarborConflictError:
            # A newer lease or policy owns the job; this worker cannot alter it.
            pass

    async def _renew(
        self, job: TopologyJob, done: asyncio.Event, heartbeat: Callable[[], None] | None
    ) -> None:
        while not done.is_set():
            if heartbeat:
                heartbeat()
            try:
                await asyncio.wait_for(done.wait(), timeout=min(30, self._lease_seconds / 3))
            except TimeoutError:
                await self._resources.repository.renew(job, lease_seconds=self._lease_seconds)

    async def _inputs(self, job: TopologyJob) -> tuple[ChunkExtractionInput, ...]:
        snapshot = await self._resources.versions.get_version(job.document_version_id)
        if snapshot is None or snapshot.chunk_artifact is None:
            raise ValueError("topology requires durable canonical chunks")
        if str(snapshot.document_id) != job.document_id:
            raise ValueError("topology version belongs to another document")
        chunks = await self._resources.chunks.get_all(
            snapshot.chunk_artifact,
            context=StorageOperationContext.system(job.tenant_id),
            verify_integrity=True,
        )
        for chunk in chunks:
            if (str(chunk.tenant_id), str(chunk.document_id), str(chunk.document_version_id)) != (
                job.tenant_id,
                job.document_id,
                job.document_version_id,
            ):
                raise ValueError("canonical chunk ownership does not match topology job")
        evidence = tuple(chunk for chunk in chunks if chunk.record_kind == RecordKind.EVIDENCE)
        if len(evidence) > self._max_chunks:
            raise ValueError("document exceeds topology chunk budget")
        return tuple(
            extraction_input(chunk, job=job) for chunk in evidence if chunk.content.strip()
        )

    async def _process(self, job: TopologyJob) -> TopologyRunResult:
        resources = self._resources
        context = StorageOperationContext.system(job.tenant_id)
        inputs = await self._inputs(job)
        await resources.repository.prepare(job, tuple(value.chunk_id for value in inputs))
        checkpoints = {item.chunk_id: item for item in await resources.repository.checkpoints(job)}
        outputs: dict[str, ExtractionOutput] = {}
        extracted = 0
        async with resources.extractor(job.policy.profile) as extractor:
            if resources.require_budget or resources.llm_operation_cost_usd is not None:
                extractor = BudgetedExtractor(
                    extractor, resources.repository, job, resources.llm_operation_cost_usd
                )
            for value in inputs:
                checkpoint = checkpoints.get(value.chunk_id)
                if checkpoint and checkpoint.input_digest != value.input_digest:
                    raise ValueError("checkpoint input changed under immutable document version")
                reference = (
                    checkpoint.artifact
                    if checkpoint
                    else await resources.artifacts.find(
                        job.policy.profile,
                        value,
                        context=context,
                    )
                )
                if reference is None:
                    reference = await ChunkExtractionRunner(resources.artifacts, extractor).run(
                        job, value
                    )
                    extracted += 1
                saved = await resources.repository.checkpoint(
                    job,
                    ChunkExtractionCheckpoint(
                        chunk_id=value.chunk_id,
                        input_digest=value.input_digest,
                        artifact=reference,
                        deployment_revision=job.policy.profile.deployment_revision,
                    ),
                )
                outputs[value.chunk_id] = await resources.artifacts.read(
                    saved.artifact,
                    value,
                    profile=job.policy.profile,
                    context=context,
                )
        mentions, _ = assemble_observations(job, outputs)
        resolved = await resources.repository.resolve_entities(
            job, tuple(sorted({item.entity_id for item in mentions}))
        )
        build_id = build_identity(job)
        content = EnrichmentBuildBuilder(job).build(inputs, outputs, resolved=resolved)
        payload = content.model_dump(mode="json")
        reference = await resources.artifacts.manifest(
            build_id,
            json.dumps(payload, sort_keys=True).encode(),
            context=context,
        )
        build = DocumentTopologyBuild.model_validate({**payload, "artifact": reference})
        await resources.repository.stage(job, build)
        await resources.repository.renew(job, lease_seconds=self._lease_seconds)
        await resources.projection.write(build, context=context)
        if not await resources.projection.verify(build, context=context):
            raise ValueError("topology graph projection verification failed")
        await resources.repository.mark_verified(job, build_id)
        if not await resources.repository.accept(job, build_id):
            return TopologyRunResult(job.job_id, "superseded", build_id)
        return TopologyRunResult(
            job.job_id, "accepted", build_id, extracted, len(inputs) - extracted
        )


def extraction_input(chunk: ChunkRecord, *, job: TopologyJob | None = None) -> ChunkExtractionInput:
    """Include all contextualization and table provenance in the reuse identity."""
    context = json.dumps(
        {
            "title": chunk.hierarchy.document_title,
            "sections": chunk.hierarchy.section_path,
            "language": chunk.language,
            "table": chunk.table_locator.model_dump(mode="json") if chunk.table_locator else None,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    value = ChunkExtractionInput(
        chunk_id=str(chunk.chunk_id), content=chunk.content, context=context
    )
    if job is not None and job.policy.profile.schema_version != "1":
        value = value.model_copy(
            update={
                "source_title": chunk.hierarchy.document_title or "",
                "heading_path": tuple(chunk.hierarchy.section_path),
                "section_ids": _section_ids(chunk),
                "source_revision": str(chunk.document_version_id),
                "processing_fingerprint": chunk.strategy_version,
                "context_dependencies": tuple(
                    dependency.model_dump_json() for dependency in job.permission_dependencies
                ),
            }
        )
    return value


def _section_ids(chunk: ChunkRecord) -> tuple[str, ...]:
    """Mirror structural projection identity for every heading prefix."""

    path = tuple(chunk.hierarchy.section_path)
    identities: list[str] = []
    for depth in range(1, len(path) + 1):
        if depth == len(path) and chunk.hierarchy.section_id is not None:
            identities.append(chunk.hierarchy.section_id)
        elif depth <= len(chunk.hierarchy.ancestry):
            identities.append(chunk.hierarchy.ancestry[depth - 1])
        elif depth + 1 == len(path) and chunk.hierarchy.parent_section_id is not None:
            identities.append(chunk.hierarchy.parent_section_id)
        else:
            identities.append(
                encoded_identifier(
                    "section",
                    {"document_id": str(chunk.document_id), "section_path": path[:depth]},
                )
            )
    return tuple(identities)


def _deferred_error(error: BaseException) -> EnrichmentDeferredError | None:
    if isinstance(error, EnrichmentDeferredError):
        return error
    if isinstance(error, BaseExceptionGroup):
        return next(
            (deferred for child in error.exceptions if (deferred := _deferred_error(child))),
            None,
        )
    return None


def _failure_code(error: BaseException) -> str:
    """Expose a root failure type without leaking provider or source text."""
    while True:
        if isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
            error = error.exceptions[0]
            continue
        original = getattr(error, "original_exception", None)
        if isinstance(original, BaseException) and original is not error:
            error = original
            continue
        break
    return type(error).__name__
