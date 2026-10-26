"""Dynamic schemas are selected by frozen configuration, never by model output."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from harborrag_adapters.topology.extraction_budget import extraction_operation_key
from harborrag_adapters.topology.extraction_schema import response_schema
from harborrag_adapters.topology.extractor import (
    LLMEntityExtractor,
    default_extraction_profile,
    extraction_request_budget,
    pinned_configuration,
)
from harborrag_core.topology import ChunkExtractionInput, ExtractionIncompleteError
from harborrag_core.topology.ontology import OntologyRegistry, RelationDefinition, builtin_ontology

from .test_extractor import config, output


def test_pinned_transport_and_reservation_bound_eight_model_dispatches():
    configuration = config()
    profile = default_extraction_profile(configuration)
    pinned = pinned_configuration(configuration, profile)
    assert pinned.retry.same_deployment_attempts == 1
    assert pinned.retry.max_deployment_failovers == 0
    assert pinned.retry.max_model_fallbacks == 0
    value = ChunkExtractionInput(chunk_id="c", content="Alpha")
    input_tokens, output_tokens = extraction_request_budget(value, profile)
    assert input_tokens > len(value.content) + profile.max_output_tokens
    assert output_tokens == 8 * profile.max_output_tokens
    longer = value.model_copy(update={"context": "é" * 1000})
    assert extraction_request_budget(longer, profile)[0] > input_tokens


def test_budget_operation_key_covers_wire_schema_and_chunk_identity():
    profile = default_extraction_profile(config())
    value = ChunkExtractionInput(chunk_id="c", content="Alpha")
    assert extraction_operation_key(value, profile) == extraction_operation_key(value, profile)
    assert extraction_operation_key(value, profile) != extraction_operation_key(
        value.model_copy(update={"chunk_id": "other"}), profile
    )
    assert extraction_operation_key(value, profile) != extraction_operation_key(
        value, profile.model_copy(update={"schema_version": "3"})
    )


def test_response_schema_requires_fused_fields_and_uses_configured_enums():
    model = response_schema(builtin_ontology())
    payload = output().model_dump(
        mode="json",
        exclude={"validation_repairs", "rejected_output_count", "rejection_reasons"},
    )
    payload.pop("retrieval_context")
    payload.pop("retrieval_context_evidence")
    assert model.model_validate(payload).title == "Alpha"
    for field in ("title", "description", "complete", "overflow"):
        with pytest.raises(ValidationError):
            model.model_validate({key: value for key, value in payload.items() if key != field})
    payload["entities"][0]["entity_type"] = "unconfigured_type"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_response_schema_restricts_fact_spans_to_chunk_content():
    model = response_schema(builtin_ontology())
    payload = output().model_dump(
        mode="json",
        exclude={"validation_repairs", "rejected_output_count", "rejection_reasons"},
    )
    payload.pop("retrieval_context")
    payload.pop("retrieval_context_evidence")
    payload["entities"][0]["span"]["source"] = "context"
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    payload = output().model_dump(
        mode="json",
        exclude={"validation_repairs", "rejected_output_count", "rejection_reasons"},
    )
    payload.pop("retrieval_context")
    payload.pop("retrieval_context_evidence")
    payload["title_evidence"][0]["source"] = "context"
    assert model.model_validate(payload).title_evidence[0].source == "context"


def test_response_schema_is_openai_strict_at_every_object_level():
    schema = response_schema(builtin_ontology()).model_json_schema()
    objects = [schema, *schema["$defs"].values()]
    for value in objects:
        if "properties" not in value:
            continue
        assert set(value["required"]) == set(value["properties"])
        assert value["additionalProperties"] is False

    assert "retrieval_context" not in schema["properties"]
    assert "retrieval_context_evidence" not in schema["properties"]


def test_configured_ontology_is_pinned_and_schema_changes_with_it():
    registry = OntologyRegistry(
        version="custom-v1",
        entity_types=("asset",),
        relations=(RelationDefinition(name="replaces", endpoint_pairs=(("asset", "asset"),)),),
    )
    profile = default_extraction_profile(config(), ontology=registry)
    assert (
        profile.schema_version,
        profile.context_policy,
        profile.code_version,
    ) == ("4", "chunk-v4-description-prefix", "11")
    assert profile.ontology == registry and profile.ontology_version == registry.version
    schema = response_schema(registry).model_json_schema()
    assert schema["$defs"]["OntologyEntity"]["properties"]["entity_type"]["const"] == "asset"
    assert schema["$defs"]["OntologyAssertion"]["properties"]["predicate"]["const"] == "replaces"
    assert profile.fingerprint != default_extraction_profile(config()).fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"complete": False}, {"overflow": True}])
async def test_one_primary_call_cannot_publish_incomplete_output(changes):
    client = SimpleNamespace(
        achat_structured=AsyncMock(return_value=output().model_copy(update=changes))
    )
    with pytest.raises(ExtractionIncompleteError):
        await LLMEntityExtractor(client).extract(
            ChunkExtractionInput(chunk_id="original", content="Alpha"),
            profile=default_extraction_profile(config()),
            tenant_id="tenant",
            document_id="document",
        )
    assert client.achat_structured.await_count == 4


@pytest.mark.asyncio
async def test_adapter_revalidates_ontology_when_injected_client_bypasses_schema():
    original = output()
    bad = original.model_copy(
        update={"entities": (original.entities[0].model_copy(update={"entity_type": "invented"}),)}
    )
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=bad))
    with pytest.raises(ValueError, match="configured ontology"):
        await LLMEntityExtractor(client).extract(
            ChunkExtractionInput(chunk_id="original", content="Alpha"),
            profile=default_extraction_profile(config()),
            tenant_id="tenant",
            document_id="document",
        )
