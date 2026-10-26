"""Pure canonical build assembly, separate from extraction and persistence."""

from dataclasses import dataclass

from harborrag_core.topology import (
    ChunkExtractionInput,
    ExtractionOutput,
    TopologyJob,
    projection_revision_for_schema,
)
from harborrag_core.topology.derived import ChunkEnrichment, ContextualManifest
from harborrag_core.topology.records import TopologyBuildContent

from .assembly import assemble_observations, build_identity


@dataclass(frozen=True)
class EnrichmentBuildBuilder:
    job: TopologyJob

    def build(
        self,
        inputs: tuple[ChunkExtractionInput, ...],
        outputs: dict[str, ExtractionOutput],
        *,
        resolved: dict[str, str],
        contextual: ContextualManifest | None = None,
    ) -> TopologyBuildContent:
        chunk_ids = tuple(value.chunk_id for value in inputs)
        if len(set(chunk_ids)) != len(chunk_ids) or set(outputs) != set(chunk_ids):
            raise ValueError("build requires exactly one extraction per distinct input chunk")
        for value in inputs:
            outputs[value.chunk_id].validate_evidence(value)
        mentions, assertions = assemble_observations(self.job, outputs)
        entity_ids = {item.entity_id for item in mentions}
        if not entity_ids <= resolved.keys() or any(
            not resolved[key].strip() for key in entity_ids
        ):
            raise ValueError("build requires a non-empty canonical resolution for every entity")
        mentions = tuple(
            item.model_copy(update={"entity_id": resolved[item.entity_id]}) for item in mentions
        )
        assertions = tuple(
            item.model_copy(
                update={
                    "subject_entity_id": resolved[item.subject_entity_id],
                    "object_entity_id": resolved[item.object_entity_id],
                }
            )
            for item in assertions
        )
        return TopologyBuildContent(
            build_id=build_identity(self.job),
            job_id=self.job.job_id,
            document_id=self.job.document_id,
            document_version_id=self.job.document_version_id,
            source_scope_id=self.job.source_scope_id,
            config_epoch=self.job.config_epoch,
            permission_dependencies=self.job.permission_dependencies,
            chunk_ids=tuple(value.chunk_id for value in inputs),
            mentions=mentions,
            assertions=assertions,
            contextual_manifest=contextual,
            representations=tuple(
                self._representation(value, outputs[value.chunk_id]) for value in inputs
            ),
            projection_revision=projection_revision_for_schema(
                self.job.policy.profile.schema_version
            ),
        )

    @staticmethod
    def _representation(value: ChunkExtractionInput, output: ExtractionOutput) -> ChunkEnrichment:
        return ChunkEnrichment(
            chunk_id=value.chunk_id,
            title=output.title,
            description=output.description,
            retrieval_context=output.retrieval_context,
            section_path=value.heading_path,
            section_ids=value.section_ids,
            validation_repairs=output.validation_repairs,
            rejected_output_count=output.rejected_output_count,
            rejection_reasons=output.rejection_reasons,
            evidence=(
                *output.title_evidence,
                *output.description_evidence,
                *output.retrieval_context_evidence,
            ),
        )
