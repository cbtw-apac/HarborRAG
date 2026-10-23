"""Shared serialization and conservative schema-plus-semantic repair reservations."""

import json

from harborrag_core.topology import ChunkExtractionInput, ExtractionProfile, digest

from .extraction_schema import response_schema
from .output_policy import OUTPUT_POLICY_VERSION

SEMANTIC_ATTEMPTS = 4
PROVIDER_CALLS_PER_SEMANTIC_ATTEMPT = 2
MAX_PROVIDER_CALLS = SEMANTIC_ATTEMPTS * PROVIDER_CALLS_PER_SEMANTIC_ATTEMPT


def extraction_operation_key(value: ChunkExtractionInput, profile: ExtractionProfile) -> str:
    """Identify the effective provider request, including its generated wire schema."""
    schema = response_schema(
        profile.resolved_ontology(), schema_version=profile.schema_version
    ).model_json_schema()
    return digest(
        {
            "profile": profile.fingerprint,
            "response_schema": schema,
            "output_policy": OUTPUT_POLICY_VERSION,
            "chunk_id": value.chunk_id,
        }
    )


def evidence_json(value: ChunkExtractionInput, profile: ExtractionProfile) -> str:
    return json.dumps(
        {
            "content": value.content,
            "context": value.context,
            "source_title": value.source_title,
            "heading_path": value.heading_path,
            "ontology": profile.resolved_ontology().model_dump(mode="json"),
        }
    )


def request_budget(
    value: ChunkExtractionInput,
    profile: ExtractionProfile,
    *,
    prompt: str,
    semantic_repair_prompt: str = "",
) -> tuple[int, int]:
    """Reserve every bounded semantic attempt and its native-schema repair.

    UTF-8 bytes upper-bound ordinary byte-tokenized supplied text. Both schema
    prompt and native-schema copies are charged conservatively, plus 4096 tokens
    per call for provider framing. The repair repeats the original prompt/schema
    and appends at most max_output_tokens from the same pinned tokenizer.
    This is a reservation, not measured billing; provider-hidden prompt additions
    beyond the framing allowance require a deployment-specific larger budget.
    """
    schema = json.dumps(
        response_schema(
            profile.resolved_ontology(), schema_version=profile.schema_version
        ).model_json_schema(),
        sort_keys=True,
        separators=(",", ":"),
    )
    schema_bytes = len(schema.encode("utf-8"))
    supplied = len(prompt.encode("utf-8")) + len(evidence_json(value, profile).encode("utf-8"))
    primary = supplied + 2 * schema_bytes + 4096
    repair_instruction = "The previous response failed JSON schema validation. Correct it and return only one JSON object matching this schema: "
    repair_extra = (
        len(repair_instruction.encode("utf-8")) + schema_bytes + max(profile.max_output_tokens, 16)
    )
    one_semantic_attempt = PROVIDER_CALLS_PER_SEMANTIC_ATTEMPT * primary + repair_extra
    semantic_retry_extra = (
        PROVIDER_CALLS_PER_SEMANTIC_ATTEMPT
        * sum(range(SEMANTIC_ATTEMPTS))
        * len(semantic_repair_prompt.encode("utf-8"))
    )
    return (
        SEMANTIC_ATTEMPTS * one_semantic_attempt + semantic_retry_extra,
        MAX_PROVIDER_CALLS * profile.max_output_tokens,
    )
