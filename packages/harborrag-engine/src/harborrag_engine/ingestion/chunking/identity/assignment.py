from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass

from harborrag_core.chunking import CanonicalIdentityBuilder, ChunkKind

from ..config import ChunkingProfile
from .fingerprint import encoded_identifier


@dataclass(frozen=True, slots=True)
class ChunkIdentity:
    """Stable logical identity paired with an immutable revision identity."""

    section_id: str
    logical_chunk_id: str
    chunk_id: str


@dataclass(frozen=True, slots=True)
class ChunkIdentityInput:
    """Typed inputs that determine one logical and immutable chunk identity."""

    document_id: str
    document_version_id: str
    strategy_version: str
    section_path: Sequence[str]
    structural_anchor: str
    local_part_index: int
    chunk_kind: ChunkKind
    content_hash: str


class ChunkIdentityBuilder(CanonicalIdentityBuilder):
    """Build domain-separated deterministic chunk and table identities."""

    def configuration_hash(
        self,
        *,
        configuration_version: str,
        profile: ChunkingProfile,
        chunker_name: str,
        chunker_version: str,
    ) -> str:
        """Hash the complete chunker configuration that affects output."""

        return encoded_identifier(
            "chunk-config",
            {
                "configuration_version": configuration_version,
                "profile": asdict(profile),
                "chunker_name": chunker_name,
                "chunker_version": chunker_version,
            },
        )

    def identify(
        self,
        values: ChunkIdentityInput,
    ) -> ChunkIdentity:
        """Generate stable logical and content-specific revision identities."""

        section_id = self.section_id(
            document_id=values.document_id,
            section_path=values.section_path,
        )
        logical_id = self.logical_chunk_id(
            section_id=section_id,
            stable_source_range={
                "anchor": values.structural_anchor,
                "part": values.local_part_index,
            },
            chunk_kind=values.chunk_kind,
        )
        chunk_id = self.chunk_id(
            logical_chunk_id=logical_id,
            document_version_id=values.document_version_id,
            strategy_version=values.strategy_version,
            content_hash=values.content_hash,
        )
        return ChunkIdentity(section_id, logical_id, chunk_id)
