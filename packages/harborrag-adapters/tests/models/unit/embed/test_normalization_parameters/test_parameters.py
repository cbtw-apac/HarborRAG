from __future__ import annotations

import pytest
from model_runtime_support import embed_config
from pydantic import SecretStr

from harborrag_adapters.models.embed.configs import HarborEmbedDefaults
from harborrag_adapters.models.embed.parameters import (
    apply_embed_defaults,
    build_embed_request,
    build_litellm_parameters,
    effective_batch_size,
    ensure_embed_request_id,
    litellm_inputs,
    normalize_embedding_inputs,
    prepare_embed_request,
)
from harborrag_adapters.models.embed.registry import HarborEmbedProvider
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import (
    EmbeddingEncodingFormat,
    EmbeddingPurpose,
    HarborEmbedMetadata,
    HarborEmbedRequest,
)
from harborrag_core.models.errors import (
    HarborEmbedConfigurationError,
    HarborEmbedInvalidRequestError,
)

from .conftest import deployment

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_embedding_input_normalization_variants_and_errors() -> None:
    assert normalize_embedding_inputs("x") == ("x",)
    assert normalize_embedding_inputs([1, 2]) == ((1, 2),)
    assert normalize_embedding_inputs(["a", "b"]) == ("a", "b")
    assert normalize_embedding_inputs(["a", [1, 2]]) == ("a", (1, 2))
    for bad in ([], [True], [object()], [[1, "bad"]]):
        with pytest.raises((TypeError, ValueError)):
            normalize_embedding_inputs(bad)


def test_build_prepare_defaults_and_identity() -> None:
    request = build_embed_request(
        ["a", "b"],
        request=None,
        model="primary",
        request_kwargs={"metadata": {"embedding_purpose": "query"}},
    )
    assert request.metadata.embedding_purpose is EmbeddingPurpose.QUERY
    existing = HarborEmbedRequest(inputs=("x",), logical_model="old")
    assert (
        build_embed_request(None, request=existing, model="new", request_kwargs={}).logical_model
        == "new"
    )
    with pytest.raises(HarborEmbedInvalidRequestError):
        build_embed_request(["x"], request=existing, model=None, request_kwargs={})
    with pytest.raises(HarborEmbedInvalidRequestError):
        build_embed_request(None, request=None, model=None, request_kwargs={})
    defaults = HarborEmbedDefaults(
        dimensions=3,
        encoding_format=EmbeddingEncodingFormat.FLOAT,
        purpose=EmbeddingPurpose.DOCUMENT,
        normalize=True,
        batch_size=2,
    )
    defaulted = apply_embed_defaults(HarborEmbedRequest(inputs=("x",)), defaults)
    assert defaulted.dimensions == 3 and defaulted.purpose is EmbeddingPurpose.DOCUMENT
    identified = ensure_embed_request_id(defaulted)
    assert identified.metadata.request_id and ensure_embed_request_id(identified) is identified
    logical, selected, prepared = prepare_embed_request(
        embed_config(), ["a"], request=None, model=None, request_kwargs={}
    )
    assert logical == "primary" and selected.name == "embed-a" and prepared.metadata.request_id
    with pytest.raises(HarborEmbedConfigurationError):
        prepare_embed_request(
            embed_config(), ["a"], request=None, model="missing", request_kwargs={}
        )


def test_embedding_parameters_batch_limits_and_purpose_mapping() -> None:
    request = HarborEmbedRequest(
        inputs=("a", (1, 2)),
        logical_model="primary",
        metadata=HarborEmbedMetadata(request_id="r", user_id="u"),
        dimensions=3,
        encoding_format=EmbeddingEncodingFormat.FLOAT,
        purpose=EmbeddingPurpose.QUERY,
        custom_headers={"X-Request": SecretStr("two")},
        extra_params={"timeout_override": 1},
        batch_size=5,
    )
    cohere = deployment(provider=HarborEmbedProvider.COHERE, model="cohere/embed")
    params = build_litellm_parameters(
        cohere,
        request,
        inputs=litellm_inputs(request),
        timeout=5,
        litellm_provider="cohere",
    )
    assert params["input_type"] == "search_query"
    assert params["extra_headers"] == {"X-Deploy": "one", "X-Request": "two"}
    assert params["input"] == ["a", [1, 2]]
    assert effective_batch_size(embed_config(deployments=(cohere,)), cohere, request) == 3
    no_batch = deployment(capabilities=HarborEmbedCapabilities(batch=False, default_dimensions=3))
    assert effective_batch_size(embed_config(deployments=(no_batch,)), no_batch, request) == 1
    for provider, key, value in [
        (HarborEmbedProvider.VOYAGE, "input_type", "query"),
        (HarborEmbedProvider.GEMINI, "task_type", "RETRIEVAL_QUERY"),
        (HarborEmbedProvider.OPENAI, None, None),
    ]:
        item = deployment(provider=provider, model=f"{provider.value}/embed")
        rendered = build_litellm_parameters(item, request, inputs=["a"], timeout=1)
        assert (rendered.get(key) if key else None) == value
    with pytest.raises(HarborEmbedInvalidRequestError):
        build_litellm_parameters(
            deployment(),
            request.model_copy(update={"extra_params": {"model": "bad"}}),
            inputs=["a"],
            timeout=1,
        )
