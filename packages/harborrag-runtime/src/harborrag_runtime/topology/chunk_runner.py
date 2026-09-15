"""Checkpoint each deterministic extraction window without changing chunk identity."""

from dataclasses import dataclass

from harborrag_adapters.topology.artifacts import ExtractionArtifacts
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.ports.topology_extraction import EntityExtractionPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import ChunkExtractionInput, ExtractionOutput, TopologyJob
from harborrag_core.topology.windowing import ExtractionWindowBuilder, merge_window_outputs


@dataclass(frozen=True)
class ChunkExtractionRunner:
    artifacts: ExtractionArtifacts
    extractor: EntityExtractionPort

    async def run(self, job: TopologyJob, value: ChunkExtractionInput) -> ArtifactReference:
        context = StorageOperationContext.system(job.tenant_id)
        windows = ExtractionWindowBuilder(job.policy.profile).build(value)
        outputs: dict[str, ExtractionOutput] = {}
        for window in windows:
            reference = await self.artifacts.find(job.policy.profile, window.input, context=context)
            if reference is None:
                output = await self.extractor.extract(
                    window.input,
                    profile=job.policy.profile,
                    tenant_id=job.tenant_id,
                    document_id=job.document_id,
                )
                reference = await self.artifacts.freeze(
                    job.policy.profile, window.input, output, context=context
                )
            outputs[window.window_id] = await self.artifacts.read(
                reference,
                window.input,
                profile=job.policy.profile,
                context=context,
            )
        merged = merge_window_outputs(value, windows, outputs)
        return await self.artifacts.freeze(job.policy.profile, value, merged, context=context)
