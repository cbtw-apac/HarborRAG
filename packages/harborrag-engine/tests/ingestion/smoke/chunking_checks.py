"""Pass criteria for the chunking smoke check.

Each function asserts one invariant a real chunking run must satisfy and
reports it as a named `SmokeCheck`, so a failing document names the invariant
it broke instead of only its exit code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chunking_stage import StageOutcome

from harborrag_engine.ingestion.chunking import ChunkingProfile
from harborrag_engine.ingestion.chunking.identity import content_fingerprint, manifest_fingerprint
from harborrag_engine.ingestion.indexing import IndexingConfig


@dataclass(frozen=True, slots=True)
class SmokeCheck:
    """One named invariant this smoke check asserts on real chunking output."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _optional_id(value: object) -> str | None:
    return str(value) if value is not None else None


def _chunks_present(outcome: StageOutcome) -> SmokeCheck:
    count = len(outcome.result.chunks)
    return SmokeCheck(
        name="chunks_present",
        passed=count > 0,
        detail=f"{count} chunk(s) produced",
    )


def _manifest_valid(outcome: StageOutcome) -> SmokeCheck:
    validation = outcome.result.manifest.validation
    return SmokeCheck(
        name="manifest_valid",
        passed=validation.valid,
        detail="; ".join(validation.errors) or "no validation errors",
    )


def _token_limits(outcome: StageOutcome, profile: ChunkingProfile) -> SmokeCheck:
    oversized = [
        record.ordinal
        for record in outcome.result.chunks
        if (record.token_count or 0) > profile.maximum_tokens
    ]
    return SmokeCheck(
        name="token_limits",
        passed=not oversized,
        detail=(
            f"ordinals over {profile.maximum_tokens} tokens: {oversized}"
            if oversized
            else f"every chunk is within {profile.maximum_tokens} tokens"
        ),
    )


def _ordinals_contiguous(outcome: StageOutcome) -> SmokeCheck:
    ordinals = [record.ordinal for record in outcome.result.chunks]
    expected = list(range(len(ordinals)))
    return SmokeCheck(
        name="ordinals_contiguous",
        passed=ordinals == expected,
        detail=f"{len(ordinals)} ordinal(s) starting at 0" if ordinals == expected else "gap found",
    )


def _content_hashes_match(outcome: StageOutcome) -> SmokeCheck:
    mismatched = [
        record.ordinal
        for record in outcome.result.chunks
        if content_fingerprint(record.content) != record.content_hash
    ]
    return SmokeCheck(
        name="content_hashes_match",
        passed=not mismatched,
        detail=f"mismatched ordinals: {mismatched}" if mismatched else "hashes match content",
    )


def _neighbor_links(outcome: StageOutcome) -> SmokeCheck:
    records = outcome.result.chunks
    broken = [
        record.ordinal
        for index, record in enumerate(records)
        if _optional_id(record.hierarchy.previous_chunk_id)
        != (str(records[index - 1].logical_chunk_id) if index else None)
        or _optional_id(record.hierarchy.next_chunk_id)
        != (str(records[index + 1].logical_chunk_id) if index + 1 < len(records) else None)
    ]
    return SmokeCheck(
        name="neighbor_links",
        passed=not broken,
        detail=f"broken ordinals: {broken}" if broken else "chunks form one ordered chain",
    )


def _manifest_matches_records(outcome: StageOutcome) -> SmokeCheck:
    manifest = outcome.result.manifest
    expected = manifest_fingerprint(reference.chunk_revision_id for reference in manifest.chunks)
    tokens = sum(record.token_count or 0 for record in outcome.result.chunks)
    passed = (
        manifest.fingerprint == expected
        and manifest.total_chunk_count == len(outcome.result.chunks)
        and manifest.total_token_count == tokens
    )
    return SmokeCheck(
        name="manifest_matches_records",
        passed=passed,
        detail=(
            f"{manifest.total_chunk_count} reference(s), {manifest.total_token_count} token(s)"
            if passed
            else "manifest counts or fingerprint disagree with the records"
        ),
    )


def _deterministic_fingerprint(outcome: StageOutcome) -> SmokeCheck:
    fingerprint = outcome.result.manifest.fingerprint
    passed = fingerprint == outcome.repeated_fingerprint
    return SmokeCheck(
        name="deterministic_fingerprint",
        passed=passed,
        detail=(
            "repeated chunking reproduced the manifest fingerprint"
            if passed
            else "repeated chunking produced a different manifest fingerprint"
        ),
    )


def _embedding_inputs_ready(outcome: StageOutcome, config: IndexingConfig) -> SmokeCheck:
    """Every chunk must render one non-empty input that fits an embedding batch."""

    limit = config.maximum_embedding_batch_tokens
    oversized = [
        prepared.record.ordinal for prepared in outcome.prepared if prepared.token_count > limit
    ]
    complete = len(outcome.prepared) == len(outcome.result.chunks)
    return SmokeCheck(
        name="embedding_inputs_ready",
        passed=complete and not oversized,
        detail=(
            f"{len(outcome.prepared)} input(s) within {limit} tokens"
            if complete and not oversized
            else f"incomplete={not complete} ordinals over {limit} tokens: {oversized}"
        ),
    )


def checks_for(
    outcome: StageOutcome,
    profile: ChunkingProfile,
    config: IndexingConfig,
) -> tuple[SmokeCheck, ...]:
    """Assert every invariant a real chunking run must satisfy."""

    return (
        _chunks_present(outcome),
        _manifest_valid(outcome),
        _token_limits(outcome, profile),
        _ordinals_contiguous(outcome),
        _content_hashes_match(outcome),
        _neighbor_links(outcome),
        _manifest_matches_records(outcome),
        _deterministic_fingerprint(outcome),
        _embedding_inputs_ready(outcome, config),
    )
