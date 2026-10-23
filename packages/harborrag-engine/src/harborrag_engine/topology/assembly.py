"""Assemble immutable observations with conservative source-backed identity."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from harborrag_core.topology.extraction import ExtractedEntity, ExtractionOutput, digest
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention, TopologyJob


def build_identity(job: TopologyJob) -> str:
    return digest({"job": job.job_id, "fence": job.fence})


def canonical_entity_id(
    tenant_id: str,
    entity: ExtractedEntity,
    *,
    source_scope_id: str,
    document_id: str,
    observation_scope_id: str,
) -> str:
    """Create a conservative identity that cannot merge unrelated documents by name.

    A grounded source-backed external identifier may join observations inside one
    configured source. Otherwise every extracted observation is distinct; reviewed
    resolution decisions are the only mechanism allowed to merge identities.
    """
    normalized_name = re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", entity.name).casefold()
    ).strip()
    external_id = entity.external_id.strip() if entity.external_id else None
    if external_id and external_id.casefold() not in entity.span.quote.casefold():
        raise ValueError("external entity identity is not grounded in its cited span")
    return str(
        uuid5(
            NAMESPACE_URL,
            digest(
                {
                    "tenant": tenant_id,
                    "entity_type": entity.entity_type.casefold(),
                    **(
                        {
                            "source_scope_id": source_scope_id,
                            "external_id": external_id,
                        }
                        if external_id
                        else {
                            "document_id": document_id,
                            "observation_scope_id": observation_scope_id,
                            "local_id": entity.local_id,
                            "normalized_name": normalized_name,
                        }
                    ),
                }
            ),
        )
    )


def assemble_observations(
    job: TopologyJob, outputs: Mapping[str, ExtractionOutput]
) -> tuple[tuple[CanonicalMention, ...], tuple[CanonicalAssertion, ...]]:
    """Assemble source observations; cross-document joins require reviewed resolution."""
    build_id = build_identity(job)
    mentions: list[CanonicalMention] = []
    assertions: list[CanonicalAssertion] = []
    for chunk_id, output in sorted(outputs.items()):
        output.validate_profile(job.policy.profile)
        entities: dict[str, str] = {}
        common = {
            "tenant_id": job.tenant_id,
            "build_id": build_id,
            "document_id": job.document_id,
            "document_version_id": job.document_version_id,
            "chunk_id": chunk_id,
        }
        for entity in output.entities:
            entity_id = canonical_entity_id(
                job.tenant_id,
                entity,
                source_scope_id=job.source_scope_id,
                document_id=job.document_id,
                observation_scope_id=chunk_id,
            )
            entities[entity.local_id] = entity_id
            mentions.append(
                CanonicalMention(
                    **common,
                    mention_id=digest([build_id, chunk_id, entity.local_id]),
                    entity_id=entity_id,
                    observation=entity,
                )
            )
        for assertion in output.assertions:
            assertions.append(
                CanonicalAssertion(
                    **common,
                    assertion_id=digest([build_id, chunk_id, assertion.local_id]),
                    subject_entity_id=entities[assertion.subject_id],
                    object_entity_id=entities[assertion.object_id],
                    observation=assertion,
                )
            )
    return tuple(mentions), tuple(assertions)
