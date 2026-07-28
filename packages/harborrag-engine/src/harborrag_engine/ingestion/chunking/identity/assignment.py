from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256

from ..config import ChunkingProfile


def _hash(prefix: str, *values: object) -> str:
    payload = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}:{sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ChunkIdentity:
    """Stable logical identity paired with an immutable revision identity."""

    logical_chunk_id: str
    chunk_revision_id: str


class ChunkIdentityService:
    """Generate logical and immutable revision identities independently."""

    def configuration_hash(
        self,
        *,
        configuration_version: str,
        profile: ChunkingProfile,
        chunker_name: str,
        chunker_version: str,
    ) -> str:
        """Hash the complete chunker configuration that affects output."""

        return _hash(
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
        *,
        tenant_id: str,
        artifact_id: str,
        strategy_name: str,
        structural_anchor: str,
        local_part_index: int,
        role: str,
        content_hash: str,
        configuration_hash: str,
        chunker_version: str,
    ) -> ChunkIdentity:
        """Generate stable logical and content-specific revision identities."""

        logical_id = _hash(
            "logical-chunk",
            tenant_id,
            artifact_id,
            strategy_name,
            structural_anchor,
            local_part_index,
            role,
        )
        revision_id = _hash(
            "chunk-revision",
            logical_id,
            content_hash,
            configuration_hash,
            chunker_version,
        )
        return ChunkIdentity(logical_id, revision_id)
