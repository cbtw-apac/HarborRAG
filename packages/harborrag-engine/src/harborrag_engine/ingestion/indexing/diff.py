from __future__ import annotations

from harborrag_engine.ingestion.chunking.schemas import ChunkManifest, ChunkReference

from .errors import ChunkDiffError
from .schemas import ChunkDiffEntry, ChunkDiffResult, ChunkDiffStatus


class IncrementalChunkDiffer:
    """Classify logical chunks without inspecting or mutating vector storage."""

    def compare(
        self,
        proposed: ChunkManifest,
        active: ChunkManifest | None,
        *,
        target_embedding_configuration_fingerprint: str,
        active_embedding_configuration_fingerprint: str | None = None,
    ) -> ChunkDiffResult:
        """Compare a proposed manifest against the currently active revision."""

        target_fingerprint = target_embedding_configuration_fingerprint.strip()
        if not target_fingerprint:
            raise ChunkDiffError("target embedding configuration fingerprint must be non-empty")
        proposed_by_id = self._references(proposed)
        if active is None:
            new_entries = tuple(
                ChunkDiffEntry(
                    logical_chunk_id=reference.logical_chunk_id,
                    status=ChunkDiffStatus.NEW,
                    previous=None,
                    current=reference,
                )
                for reference in proposed.chunks
            )
            return ChunkDiffResult(
                entries=new_entries,
                active_manifest_fingerprint=None,
                proposed_manifest_fingerprint=proposed.fingerprint,
                active_embedding_configuration_fingerprint=None,
                target_embedding_configuration_fingerprint=target_fingerprint,
            )

        self._assert_same_artifact(active, proposed)
        active_by_id = self._references(active)
        model_changed = active_embedding_configuration_fingerprint != target_fingerprint
        entries: list[ChunkDiffEntry] = []
        for reference in proposed.chunks:
            previous = active_by_id.get(reference.logical_chunk_id)
            if previous is None:
                status = ChunkDiffStatus.NEW
            elif previous.content_hash != reference.content_hash:
                status = ChunkDiffStatus.CHANGED
            elif model_changed:
                status = ChunkDiffStatus.REEMBED_REQUIRED
            else:
                status = ChunkDiffStatus.UNCHANGED
            entries.append(
                ChunkDiffEntry(
                    logical_chunk_id=reference.logical_chunk_id,
                    status=status,
                    previous=previous,
                    current=reference,
                )
            )
        entries.extend(
            ChunkDiffEntry(
                logical_chunk_id=reference.logical_chunk_id,
                status=ChunkDiffStatus.REMOVED,
                previous=reference,
                current=None,
            )
            for reference in active.chunks
            if reference.logical_chunk_id not in proposed_by_id
        )
        return ChunkDiffResult(
            entries=tuple(entries),
            active_manifest_fingerprint=active.fingerprint,
            proposed_manifest_fingerprint=proposed.fingerprint,
            active_embedding_configuration_fingerprint=(active_embedding_configuration_fingerprint),
            target_embedding_configuration_fingerprint=target_fingerprint,
        )

    @staticmethod
    def _references(manifest: ChunkManifest) -> dict[str, ChunkReference]:
        references = {reference.logical_chunk_id: reference for reference in manifest.chunks}
        if len(references) != len(manifest.chunks):
            raise ChunkDiffError("chunk manifest contains duplicate logical chunk IDs")
        return references

    @staticmethod
    def _assert_same_artifact(active: ChunkManifest, proposed: ChunkManifest) -> None:
        if active.tenant_id != proposed.tenant_id or active.artifact_id != proposed.artifact_id:
            raise ChunkDiffError(
                "active and proposed chunk manifests must describe the same tenant artifact"
            )
