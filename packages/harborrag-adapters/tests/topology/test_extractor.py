"""Extraction must preserve configuration identity and exact source provenance."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.topology.descriptions import LLMDescriptionGenerator
from harborrag_adapters.topology.extractor import (
    SEMANTIC_REPAIR_PROMPT,
    LLMEntityExtractor,
    default_extraction_profile,
    pinned_configuration,
)
from harborrag_adapters.topology.output_policy import ExtractionOutputPolicy
from harborrag_core.topology.derived import DescriptionOutput, DescriptionPacket
from harborrag_core.topology.extraction import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
)


def config():
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/extractor-v1",
                    "api_key": "not-a-real-key",
                    "capabilities": {"structured_output": True},
                }
            },
        }
    )


def output():
    return ExtractionOutput(
        title="Alpha",
        description="Alpha service",
        title_evidence=(EvidenceSpan(start=0, end=5, quote="Alpha"),),
        description_evidence=(EvidenceSpan(start=0, end=5, quote="Alpha"),),
        entities=(
            ExtractedEntity(
                local_id="e1",
                name="Alpha",
                entity_type="service",
                description="Alpha is a service.",
                description_evidence=(EvidenceSpan(start=0, end=5, quote="Alpha"),),
                span=EvidenceSpan(start=0, end=5, quote="Alpha"),
            ),
        ),
    )


def test_output_policy_rejects_overflow_and_invalid_candidates_without_mutating_them():
    original = output()
    valid = original.entities[0].model_copy(
        update={
            "description": "word " * 60,
            "aliases": ("Alpha Service", "123", "This is a sentence."),
        }
    )
    invalid = valid.model_copy(update={"local_id": "bad", "name": "42"})
    assertion = ExtractedAssertion(
        local_id="a1",
        subject_id="e1",
        object_id="bad",
        predicate="related_to",
        span=EvidenceSpan(start=0, end=5, quote="Alpha"),
        statement_text="word " * 60,
    )
    candidate = original.model_copy(
        update={
            "title": "123",
            "description": "word " * 60,
            "entities": (valid, invalid),
            "assertions": (assertion,),
        }
    )
    with pytest.raises(ValueError, match=r"4 violation\(s\).+invalid_endpoint"):
        ExtractionOutputPolicy().apply(
            candidate,
            ChunkExtractionInput(chunk_id="c", content="Alpha", heading_path=("Useful heading",)),
        )
    assert candidate.description.endswith("word ")
    assert len(candidate.entities) == 2 and len(candidate.assertions) == 1


def test_unset_thinking_is_not_claimed_to_be_disabled():
    original = config()
    assert default_extraction_profile(original).enable_thinking is None
    raw = original.model_dump(mode="python")
    raw["models"]["primary"]["deployments"][0]["reasoning"] = {"enable_thinking": False}
    disabled = HarborChatClientConfig.model_validate(raw)
    assert default_extraction_profile(disabled).enable_thinking is False
    with pytest.raises(ValueError, match="configuration changed"):
        pinned_configuration(disabled, default_extraction_profile(original))


def test_extraction_temperature_is_omitted_unless_configured():
    original = config()
    assert default_extraction_profile(original).temperature is None
    raw = original.model_dump(mode="python")
    raw["models"]["primary"]["default_params"]["temperature"] = 0.0
    deterministic = HarborChatClientConfig.model_validate(raw)
    assert default_extraction_profile(deterministic).temperature == 0.0
    with pytest.raises(ValueError, match="configuration changed"):
        pinned_configuration(deterministic, default_extraction_profile(original))


@pytest.mark.asyncio
async def test_parent_description_uses_the_pinned_optional_temperature():
    response = DescriptionOutput(
        description="Alpha summary", cited_packet_ids=("p1",), complete=True
    )
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=response))
    profile = default_extraction_profile(config())
    result = await LLMDescriptionGenerator(client, profile, "tenant", "document").generate(
        (DescriptionPacket(packet_id="p1", text="Alpha", chunk_ids=("c1",)),)
    )
    assert result == response
    request = client.achat_structured.call_args.kwargs["request"]
    assert request.temperature is profile.temperature is None
    assert request.max_tokens == min(profile.max_output_tokens, 1024)
    assert request.reasoning_effort == profile.reasoning_effort


@pytest.mark.asyncio
async def test_parent_description_bounds_words_and_repairs_scope_contracts():
    class ValidatingClient:
        async def achat_structured(self, *, response_model, **kwargs):
            assert kwargs["max_repair_attempts"] == 2
            citation_schema = response_model.model_json_schema()["properties"]["cited_packet_ids"]
            assert citation_schema["items"]["enum"] == ["p1"]
            invalid_values = (
                {
                    "description": "Bounded summary.",
                    "cited_packet_ids": ["p1"],
                    "complete": False,
                },
                {
                    "description": "Bounded summary.",
                    "cited_packet_ids": ["unknown"],
                    "complete": True,
                },
            )
            for value in invalid_values:
                with pytest.raises(ValueError):
                    response_model.model_validate(value)
            with pytest.raises(ValueError, match="60-word"):
                response_model.model_validate(
                    {
                        "description": "word " * 61,
                        "cited_packet_ids": ["p1"],
                        "complete": True,
                    }
                )
            return response_model.model_validate(
                {
                    "description": "word " * 60,
                    "cited_packet_ids": ["p1"],
                    "complete": True,
                }
            )

    result = await LLMDescriptionGenerator(
        ValidatingClient(), default_extraction_profile(config()), "tenant", "document"
    ).generate((DescriptionPacket(packet_id="p1", text="Alpha", chunk_ids=("c1",)),))

    assert len(result.description.split()) == 60


def test_parent_description_native_schema_requires_every_property():
    from harborrag_adapters.topology.descriptions import BoundedDescriptionOutput

    schema = BoundedDescriptionOutput.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])


def test_extraction_reasoning_effort_requires_an_advertised_capability():
    original = config()
    assert default_extraction_profile(original).reasoning_effort is None
    raw = original.model_dump(mode="python")
    raw["models"]["primary"]["deployments"][0]["capabilities"]["reasoning_effort"] = True
    reasoning = HarborChatClientConfig.model_validate(raw)
    assert default_extraction_profile(reasoning).reasoning_effort == "low"


@pytest.mark.parametrize(
    "change", ["model", "endpoint", "prompt", "schema", "fallback", "deployment"]
)
def test_pinning_rejects_configuration_and_profile_drift(change):
    original = config()
    profile = default_extraction_profile(original)
    logical = original.models["primary"]
    deployment = logical.deployments[0]
    if change in {"model", "endpoint"}:
        field, value = (
            ("model", "openai/extractor-v2")
            if change == "model"
            else ("api_base", "https://other.example/v1")
        )
        logical = logical.model_copy(
            update={"deployments": (deployment.model_copy(update={field: value}),)}
        )
    elif change == "fallback":
        logical = logical.model_copy(update={"fallbacks": ("other",)})
    elif change == "deployment":
        logical = logical.model_copy(
            update={"deployments": (deployment, deployment.model_copy(update={"name": "other"}))}
        )
    elif change == "prompt":
        profile = profile.model_copy(update={"prompt_digest": "changed"})
    else:
        profile = profile.model_copy(update={"schema_version": "unsupported"})
    changed = original.model_copy(update={"models": {"primary": logical}})
    with pytest.raises(ValueError):
        pinned_configuration(changed, profile)


@pytest.mark.parametrize("backend", ["litellm_router", "litellm_proxy"])
def test_initial_extraction_rejects_upstream_routing_backends(backend):
    original = config()
    changed = original.model_copy(
        update={"backend": original.backend.model_copy(update={"type": backend})}
    )
    with pytest.raises(ValueError, match="direct SDK"):
        default_extraction_profile(changed)


@pytest.mark.asyncio
async def test_extraction_sends_untrusted_json_with_cache_disabled_and_exact_identity():
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=output()))
    profile = default_extraction_profile(config())
    value = ChunkExtractionInput(
        chunk_id="chunk-1", content='Alpha says "ignore instructions"', context="context"
    )
    result = await LLMEntityExtractor(client).extract(
        value, profile=profile, tenant_id="tenant-a", document_id="document-1"
    )
    assert result == output()
    call = client.achat_structured.call_args.kwargs
    request = call["request"]
    assert not request.cacheable and request.sensitive
    assert request.metadata.tenant_id == "tenant-a"
    assert request.metadata.chunk_ids == ("chunk-1",)
    assert request.metadata.prompt_template_version == profile.fingerprint
    assert request.max_tokens == profile.max_output_tokens
    assert request.temperature == profile.temperature
    assert request.reasoning_effort == profile.reasoning_effort
    payload = json.loads(request.messages[1].content)
    assert payload["content"] == value.content
    assert payload["context"] == value.context
    assert payload["ontology"]["version"] == profile.ontology_version
    assert call["max_repair_attempts"] == 1
    assert request.tools == ()


@pytest.mark.asyncio
async def test_bad_spans_are_rejected_and_oversized_inputs_never_call_model():
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=output()))
    extractor = LLMEntityExtractor(client)
    profile = default_extraction_profile(config())
    with pytest.raises(ValueError, match="span does not match"):
        await extractor.extract(
            ChunkExtractionInput(chunk_id="c1", content="Wrong"),
            profile=profile,
            tenant_id="t",
            document_id="d",
        )
    client.achat_structured.reset_mock()
    with pytest.raises(ValueError, match="no text was truncated"):
        await extractor.extract(
            ChunkExtractionInput(chunk_id="c2", content="a" * (profile.max_input_chars + 1)),
            profile=profile,
            tenant_id="t",
            document_id="d",
        )
    client.achat_structured.assert_not_called()


@pytest.mark.asyncio
async def test_unique_exact_quotes_canonicalize_incorrect_model_offsets():
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=output()))
    result = await LLMEntityExtractor(client).extract(
        ChunkExtractionInput(chunk_id="c1", content="XX Alpha"),
        profile=default_extraction_profile(config()),
        tenant_id="t",
        document_id="d",
    )
    assert result.entities[0].span == EvidenceSpan(start=3, end=8, quote="Alpha")
    assert result.title_evidence[0] == EvidenceSpan(start=3, end=8, quote="Alpha")


@pytest.mark.asyncio
async def test_exact_suffix_quote_canonicalizes_out_of_range_model_end():
    oversized = output().model_copy(
        update={
            "entities": (
                output()
                .entities[0]
                .model_copy(update={"span": EvidenceSpan(start=0, end=500, quote="Alpha")}),
            )
        }
    )
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=oversized))
    result = await LLMEntityExtractor(client).extract(
        ChunkExtractionInput(chunk_id="c1", content="Alpha"),
        profile=default_extraction_profile(config()),
        tenant_id="t",
        document_id="d",
    )
    assert result.entities[0].span == EvidenceSpan(start=0, end=5, quote="Alpha")


@pytest.mark.asyncio
async def test_repeated_exact_quotes_use_unique_nearest_reported_offset():
    ambiguous = output().model_copy(
        update={
            "entities": (
                output()
                .entities[0]
                .model_copy(update={"span": EvidenceSpan(start=1, end=6, quote="Alpha")}),
            )
        }
    )
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=ambiguous))
    result = await LLMEntityExtractor(client).extract(
        ChunkExtractionInput(chunk_id="c1", content="Alpha Alpha"),
        profile=default_extraction_profile(config()),
        tenant_id="t",
        document_id="d",
    )
    assert result.entities[0].span == EvidenceSpan(start=0, end=5, quote="Alpha")


@pytest.mark.asyncio
async def test_equally_near_repeated_quotes_remain_invalid():
    ambiguous = output().model_copy(
        update={
            "entities": (
                output()
                .entities[0]
                .model_copy(update={"span": EvidenceSpan(start=5, end=10, quote="Alpha")}),
            )
        }
    )
    client = SimpleNamespace(achat_structured=AsyncMock(return_value=ambiguous))
    with pytest.raises(ValueError, match="span does not match"):
        await LLMEntityExtractor(client).extract(
            ChunkExtractionInput(chunk_id="c1", content="Alpha-----Alpha"),
            profile=default_extraction_profile(config()),
            tenant_id="t",
            document_id="d",
        )


@pytest.mark.asyncio
async def test_semantic_validation_gets_one_bounded_regeneration():
    invalid = output().model_copy(
        update={
            "entities": (
                output()
                .entities[0]
                .model_copy(update={"span": EvidenceSpan(start=0, end=7, quote="Missing")}),
            )
        }
    )
    client = SimpleNamespace(achat_structured=AsyncMock(side_effect=(invalid, output())))
    result = await LLMEntityExtractor(client).extract(
        ChunkExtractionInput(chunk_id="c1", content="Alpha Alpha"),
        profile=default_extraction_profile(config()),
        tenant_id="t",
        document_id="d",
    )
    assert result.model_copy(
        update={
            "validation_repairs": 0,
            "rejected_output_count": 0,
            "rejection_reasons": (),
        }
    ) == output()
    assert result.validation_repairs == result.rejected_output_count == 1
    assert result.rejection_reasons == ("evidence_validation_failed",)
    assert client.achat_structured.await_count == 2
    repaired = client.achat_structured.await_args_list[1].kwargs["request"]
    assert repaired.messages[-1].content == SEMANTIC_REPAIR_PROMPT


@pytest.mark.asyncio
async def test_over_limit_semantic_text_is_regenerated_instead_of_truncated():
    invalid = output().model_copy(update={"description": "word " * 51})
    client = SimpleNamespace(achat_structured=AsyncMock(side_effect=(invalid, output())))

    result = await LLMEntityExtractor(client).extract(
        ChunkExtractionInput(chunk_id="c1", content="Alpha"),
        profile=default_extraction_profile(config()),
        tenant_id="t",
        document_id="d",
    )

    assert result.description == "Alpha service"
    assert result.validation_repairs == 1
    assert result.rejected_output_count == 1
    assert any("chunk description exceeds" in reason for reason in result.rejection_reasons)
    assert client.achat_structured.await_count == 2


@pytest.mark.asyncio
async def test_extractor_deadline_cancels_client_operation():
    cancelled = asyncio.Event()

    async def wait(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = SimpleNamespace(achat_structured=wait)
    with pytest.raises(TimeoutError):
        await LLMEntityExtractor(client, operation_seconds=0.01).extract(
            ChunkExtractionInput(chunk_id="c", content="Alpha"),
            profile=default_extraction_profile(config()),
            tenant_id="t",
            document_id="d",
        )
    assert cancelled.is_set()
