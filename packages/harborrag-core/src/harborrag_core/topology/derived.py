"""Generated views are separate from immutable source evidence and raw vectors."""

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from harborrag_core.base import StrictModel
from harborrag_core.ingestion import ArtifactReference

from .extraction import EvidenceSpan, digest
from .text_policy import PARENT_DESCRIPTION_MAX_WORDS, enforce_text_budget


class ChunkEnrichment(StrictModel):
    chunk_id: str = Field(min_length=1, max_length=256)
    title: str = Field(default="", max_length=512)
    description: str = Field(default="", max_length=4000)
    retrieval_context: str = Field(default="", max_length=2000)
    evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=300)
    section_path: tuple[str, ...] = Field(default=(), max_length=50)
    section_ids: tuple[str, ...] = Field(default=(), max_length=50)
    validation_repairs: int = Field(default=0, ge=0, le=8)
    rejected_output_count: int = Field(default=0, ge=0, le=8)
    rejection_reasons: tuple[str, ...] = Field(default=(), max_length=32)


class RollupSource(StrictModel):
    """One child's text and its position, for rolling up into a parent description.

    `text` is the child's canonical content. Extraction output is not required: a parent
    summarises what its children say, not what a prior model said about them.
    """

    chunk_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1)
    section_path: tuple[str, ...] = Field(default=(), max_length=50)
    section_ids: tuple[str, ...] = Field(default=(), max_length=50)

    @classmethod
    def from_enrichment(cls, chunk: ChunkEnrichment) -> "RollupSource":
        """Adapt an already-enriched chunk, for callers that have one."""

        return cls(
            chunk_id=chunk.chunk_id,
            text=chunk.description,
            section_path=chunk.section_path,
            section_ids=chunk.section_ids,
        )


class ContextualManifest(StrictModel):
    artifact: ArtifactReference
    embedding_profile: str = Field(min_length=1, max_length=128)
    dimension: int = Field(ge=1)
    point_ids: tuple[str, ...] = Field(max_length=10000)
    index_name: str = "contextual-evidence-v2"


class ContextualIndexProfile(StrictModel):
    model: str = Field(min_length=1)
    dimension: int = Field(ge=1)
    deployment_revision: str = Field(min_length=1)
    max_input_bytes: int = Field(default=8000, ge=100, le=100000)
    storage_revision: Literal["float32-unit-dot-v1"] = "float32-unit-dot-v1"
    context_recipe: Literal["description-plus-content-v1"] = "description-plus-content-v1"
    description_revision: str = Field(
        default="parent-v3-profile-output-required-complete",
        min_length=1,
        max_length=128,
    )

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"description_revision"}))

    @property
    def index_name(self) -> str:
        return f"contextual-v2-{self.fingerprint[:24]}"

    @property
    def parent_fingerprint(self) -> str:
        return digest([self.fingerprint, self.description_revision])

    @property
    def parent_index_name(self) -> str:
        return f"parent-v2-{self.parent_fingerprint[:24]}"


@dataclass(frozen=True, slots=True)
class DerivedVectorProduct:
    """One vector-publishing derived product and every name it is known by.

    These four facets were previously spread across three uncoordinated dicts
    (cleanup prefixes, projection record kinds, maintenance fingerprints). Omitting a
    product from any one of them leaks vector points with no error at write, read or
    cleanup time, so they are declared once here.
    """

    artifact_kind: str
    record_kind: str
    index_prefix: str
    profile_attribute: str

    def fingerprint(self, profile: ContextualIndexProfile) -> str:
        return str(getattr(profile, self.profile_attribute))

    def index_name(self, fingerprint: str) -> str:
        return f"{self.index_prefix}{fingerprint[:24]}"


# `record_kind` intentionally differs from `artifact_kind` for contextual chunks: the
# vector payload says "contextual" while the derivation row says "contextual_chunk".
DERIVED_VECTOR_PRODUCTS: tuple[DerivedVectorProduct, ...] = (
    DerivedVectorProduct(
        artifact_kind="contextual_chunk",
        record_kind="contextual",
        index_prefix="contextual-v2-",
        profile_attribute="fingerprint",
    ),
    DerivedVectorProduct(
        artifact_kind="parent_description",
        record_kind="parent_description",
        index_prefix="parent-v2-",
        profile_attribute="parent_fingerprint",
    ),
)


class DescriptionPacket(StrictModel):
    packet_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=24000)
    chunk_ids: tuple[str, ...] = Field(min_length=1, max_length=10000)
    cited_chunk_ids: tuple[str, ...] = Field(default=(), max_length=10000)

    def prompt_payload(self) -> dict[str, str]:
        """Return only model-visible fields; canonical lineage stays outside the prompt."""

        return {"packet_id": self.packet_id, "text": self.text}


def description_prompt_json(packets: tuple[DescriptionPacket, ...]) -> str:
    """Serialize the single canonical model-visible parent-summary payload."""

    return json.dumps(
        [packet.prompt_payload() for packet in packets],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ParentDescription(StrictModel):
    parent_key: str
    level: Literal["section", "structure", "document", "folder"]
    section_path: tuple[str, ...] = Field(default=(), max_length=50)
    structure_id: str | None = Field(default=None, min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=8000)
    input_chunk_ids: tuple[str, ...] = Field(min_length=1, max_length=10000)
    cited_chunk_ids: tuple[str, ...] = Field(min_length=1, max_length=10000)
    input_digest: str
    complete: bool = True
    input_coverage: Literal["complete"] = "complete"
    semantic_coverage: Literal["not_evaluated", "validated"] = "not_evaluated"


class DescriptionOutput(StrictModel):
    description: str = Field(min_length=1, max_length=8000)
    cited_packet_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    complete: bool
    topics: tuple[str, ...] = Field(default=(), max_length=12)
    key_entities: tuple[str, ...] = Field(default=(), max_length=12)
    content_types: tuple[str, ...] = Field(default=(), max_length=8)

    def require_bounded_description(self) -> None:
        enforce_text_budget(
            self.description,
            field="parent description",
            max_words=PARENT_DESCRIPTION_MAX_WORDS,
        )
