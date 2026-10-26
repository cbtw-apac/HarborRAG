"""ACL-safe presentation of canonical LLM topology and its evidence."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.topology import (
    CanonicalAssertion,
    CanonicalMention,
    ChunkExtractionCheckpoint,
    DocumentTopologyBuild,
    TopologyJob,
)
from harborrag_core.topology.derived import ChunkEnrichment


@dataclass(frozen=True)
class TopologyInspectionBuilder:
    """Join projected descriptions, canonical facts, and frozen model provenance."""

    topology: DocumentTopologyBuild
    job: TopologyJob
    checkpoints: tuple[ChunkExtractionCheckpoint, ...]
    resolved_deployment: dict[str, object] | None = None

    def build(self, *, limit: int) -> dict[str, object]:
        bounded = max(1, min(limit, 100))
        mentions = self.topology.mentions[:bounded]
        assertions = self.topology.assertions[:bounded]
        representations = self.topology.representations[:bounded]
        checkpoints = {item.chunk_id: item for item in self.checkpoints}
        entities = {
            (item.chunk_id, item.observation.local_id): item for item in self.topology.mentions
        }
        profile = self.job.policy.profile
        return {
            "build": {
                "build_id": self.topology.build_id,
                "job_id": self.topology.job_id,
                "document_id": self.topology.document_id,
                "document_version_id": self.topology.document_version_id,
                "source_scope_id": self.topology.source_scope_id,
                "projection_revision": self.topology.projection_revision,
                "artifact_sha256": self.topology.artifact.sha256,
                "config_epoch": self.topology.config_epoch,
            },
            "generation": {
                "method": "llm_structured_extraction",
                "model": profile.model,
                "deployment_revision": profile.deployment_revision,
                "extraction_fingerprint": profile.fingerprint,
                "prompt_digest": profile.prompt_digest,
                "schema_version": profile.schema_version,
                "ontology_version": profile.ontology_version,
                "context_policy": profile.context_policy,
                "code_version": profile.code_version,
                "enable_thinking": profile.enable_thinking,
                "reasoning_effort": profile.reasoning_effort,
                "temperature": profile.temperature,
                "checkpoint_count": len(self.checkpoints),
                "checkpoint_kind": "immutable_structured_extraction",
                "resolved_deployment": self.resolved_deployment or {"configuration_match": False},
            },
            "totals": {
                "chunks": len(self.topology.chunk_ids),
                "representations": len(self.topology.representations),
                "mentions": len(self.topology.mentions),
                "assertions": len(self.topology.assertions),
                "validation_repairs": sum(
                    item.validation_repairs for item in self.topology.representations
                ),
                "rejected_model_outputs": sum(
                    item.rejected_output_count for item in self.topology.representations
                ),
            },
            "returned": {
                "representations": len(representations),
                "mentions": len(mentions),
                "assertions": len(assertions),
                "limit_per_kind": bounded,
            },
            "representations": [
                self._representation(item, checkpoints.get(item.chunk_id))
                for item in representations
            ],
            "mentions": [self._mention(item) for item in mentions],
            "assertions": [self._assertion(item, entities) for item in assertions],
        }

    @staticmethod
    def _representation(
        item: ChunkEnrichment, checkpoint: ChunkExtractionCheckpoint | None
    ) -> dict[str, object]:
        # ``retrieval_context`` belongs to the legacy v3 extraction contract.
        # Frozen builds must remain readable, but semantic-v6 inspection should
        # not expose a retired empty field as though it were a current product.
        generated = {
            "title": item.title,
            "description": item.description,
        }
        if item.retrieval_context:
            generated["retrieval_context"] = item.retrieval_context
        result: dict[str, object] = {
            "chunk_id": item.chunk_id,
            "generated": generated,
            "supporting_evidence": [span.model_dump(mode="json") for span in item.evidence],
            "semantic_validation": {
                "repairs": item.validation_repairs,
                "rejected_outputs": item.rejected_output_count,
                "reasons": list(item.rejection_reasons),
            },
        }
        if checkpoint is not None:
            result["checkpoint"] = {
                "input_digest": checkpoint.input_digest,
                "artifact_sha256": checkpoint.artifact.sha256,
                "deployment_revision": checkpoint.deployment_revision,
            }
        return result

    @staticmethod
    def _mention(item: CanonicalMention) -> dict[str, object]:
        observation = item.observation
        return {
            "mention_id": item.mention_id,
            "entity_id": item.entity_id,
            "chunk_id": item.chunk_id,
            "name": observation.name,
            "entity_type": observation.entity_type,
            "aliases": list(observation.aliases),
            "external_id": observation.external_id,
            "evidence": observation.span.model_dump(mode="json"),
        }

    @staticmethod
    def _assertion(
        item: CanonicalAssertion,
        entities: dict[tuple[str, str], CanonicalMention],
    ) -> dict[str, object]:
        observation = item.observation
        subject = entities.get((item.chunk_id, observation.subject_id))
        object_ = entities.get((item.chunk_id, observation.object_id))
        return {
            "assertion_id": item.assertion_id,
            "chunk_id": item.chunk_id,
            "subject": TopologyInspectionBuilder._endpoint(
                item.subject_entity_id, observation.subject_id, subject
            ),
            "predicate": observation.predicate,
            "object": TopologyInspectionBuilder._endpoint(
                item.object_entity_id, observation.object_id, object_
            ),
            "statement_text": observation.statement_text,
            "polarity": observation.polarity,
            "modality": observation.modality,
            "attribution": observation.attribution,
            "qualifiers": list(observation.qualifiers),
            "valid_from": observation.valid_from,
            "valid_to": observation.valid_to,
            "temporal_precision": observation.temporal_precision,
            "time_qualifier": observation.time_qualifier,
            "evidence": observation.span.model_dump(mode="json"),
        }

    @staticmethod
    def _endpoint(
        entity_id: str, local_id: str, mention: CanonicalMention | None
    ) -> dict[str, object]:
        observation = mention.observation if mention is not None else None
        return {
            "entity_id": entity_id,
            "local_id": local_id,
            "name": observation.name if observation is not None else None,
            "entity_type": observation.entity_type if observation is not None else None,
        }
