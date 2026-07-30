from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from harborrag_core.chunking import ChunkKind

from ..config import ChunkingProfile
from ..hierarchy import normalize_section_path
from .fingerprint import encoded_identifier


@dataclass(frozen=True, slots=True)
class ChunkIdentity:
    """Stable logical identity paired with an immutable revision identity."""

    section_id: str
    logical_chunk_id: str
    chunk_id: str

    @property
    def chunk_revision_id(self) -> str:
        """Compatibility access for the former exact chunk identity name."""

        return self.chunk_id


class ChunkIdentityBuilder:
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

    def section_id(
        self,
        *,
        document_id: str,
        section_path: Sequence[str],
    ) -> str:
        """Return the stable identity of a logical document section."""

        return encoded_identifier(
            "section",
            {
                "document_id": document_id,
                "section_path": normalize_section_path(section_path),
            },
        )

    def logical_chunk_id(
        self,
        *,
        section_id: str,
        stable_source_range: Mapping[str, object],
        chunk_kind: ChunkKind,
    ) -> str:
        """Return stable conceptual identity independent of content versions."""

        return encoded_identifier(
            "logical-chunk",
            {
                "section_id": section_id,
                "stable_source_range": dict(stable_source_range),
                "chunk_kind": chunk_kind.value,
            },
        )

    def chunk_id(
        self,
        *,
        logical_chunk_id: str,
        document_version_id: str,
        strategy_version: str,
        content_hash: str,
    ) -> str:
        """Return exact identity for content under one document and strategy version."""

        return encoded_identifier(
            "chunk",
            {
                "logical_chunk_id": logical_chunk_id,
                "document_version_id": document_version_id,
                "strategy_version": strategy_version,
                "content_hash": content_hash,
            },
        )

    def table_id(
        self,
        *,
        document_id: str,
        section_path: Sequence[str],
        stable_table_location: Mapping[str, object],
    ) -> str:
        """Return stable identity for one logical table location."""

        return encoded_identifier(
            "table",
            {
                "document_id": document_id,
                "section_path": normalize_section_path(section_path),
                "stable_table_location": dict(stable_table_location),
            },
        )

    def table_version_id(
        self,
        *,
        table_id: str,
        source_version: str,
        content_hash: str,
    ) -> str:
        """Return exact identity for a version of one logical table."""

        return encoded_identifier(
            "table-version",
            {
                "table_id": table_id,
                "source_version": source_version,
                "content_hash": content_hash,
            },
        )

    def permission_set_id(
        self,
        *,
        tenant_id: str,
        permissions: Mapping[str, object],
    ) -> str:
        """Hash permission data so canonical records never expose raw ACL values."""

        return encoded_identifier(
            "permission-set",
            {"tenant_id": tenant_id, "permissions": dict(permissions)},
        )

    def identify(
        self,
        *,
        document_id: str,
        document_version_id: str,
        strategy_version: str,
        section_path: Sequence[str],
        structural_anchor: str,
        local_part_index: int,
        chunk_kind: ChunkKind,
        content_hash: str,
    ) -> ChunkIdentity:
        """Generate stable logical and content-specific revision identities."""

        section_id = self.section_id(
            document_id=document_id,
            section_path=section_path,
        )
        logical_id = self.logical_chunk_id(
            section_id=section_id,
            stable_source_range={
                "anchor": structural_anchor,
                "part": local_part_index,
            },
            chunk_kind=chunk_kind,
        )
        chunk_id = self.chunk_id(
            logical_chunk_id=logical_id,
            document_version_id=document_version_id,
            strategy_version=strategy_version,
            content_hash=content_hash,
        )
        return ChunkIdentity(section_id, logical_id, chunk_id)


class ChunkIdentityService(ChunkIdentityBuilder):
    """Compatibility name for the existing engine identity collaborator."""
