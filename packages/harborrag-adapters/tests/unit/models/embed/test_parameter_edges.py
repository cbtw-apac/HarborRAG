from __future__ import annotations

from typing import Any

import pytest
from harborrag_adapters.models.embed import HarborEmbedClientConfig
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


def _config(
    provider: str = "openai",
    *,
    deployment_updates: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    client_updates: dict[str, Any] | None = None,
) -> HarborEmbedClientConfig:
    deployment: dict[str, Any] = {
        "name": "primary-0",
        "provider": provider,
        "model": f"{provider}/embed-model",
        "api_key": "key",
    }
    deployment.update(deployment_updates or {})
    document: dict[str, Any] = {
        "default_model": "primary",
        "models": {
            "primary": {
                "deployments": [deployment],
                "default_params": defaults or {},
            }
        },
    }
    document.update(client_updates or {})
    return HarborEmbedClientConfig.from_dict(document)


def test_input_normalization_accepts_mixed_batches_and_rejects_invalid_values() -> None:
    assert normalize_embedding_inputs("one") == ("one",)
    assert normalize_embedding_inputs([1, 2]) == ((1, 2),)
    assert normalize_embedding_inputs(["one", "two"]) == ("one", "two")
    assert normalize_embedding_inputs(["one", [1, 2]]) == ("one", (1, 2))

    with pytest.raises(ValueError, match="empty"):
        normalize_embedding_inputs([])
    with pytest.raises(TypeError, match="strings or token arrays"):
        normalize_embedding_inputs([object()])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="strings or token arrays"):
        normalize_embedding_inputs([[True]])


def test_request_builder_handles_core_requests_metadata_and_conflicts() -> None:
    metadata = HarborEmbedMetadata(request_id="existing")
    request = HarborEmbedRequest(inputs=("one",), metadata=metadata)
    copied = build_embed_request(
        None,
        request=request,
        model="override",
        request_kwargs={},
    )
    assert copied.logical_model == "override"
    assert ensure_embed_request_id(copied) is copied

    built = build_embed_request(
        ["one"],
        request=None,
        model="primary",
        request_kwargs={"metadata": metadata, "user": "user"},
    )
    assert built.metadata is metadata
    assert built.user == "user"

    with pytest.raises(HarborEmbedInvalidRequestError, match="cannot be combined"):
        build_embed_request(["two"], request=request, model=None, request_kwargs={})
    with pytest.raises(HarborEmbedInvalidRequestError, match="required"):
        build_embed_request(None, request=None, model=None, request_kwargs={})


def test_prepare_request_maps_input_and_model_resolution_errors() -> None:
    config = _config()
    with pytest.raises(HarborEmbedInvalidRequestError, match="invalid embedding"):
        prepare_embed_request(
            config,
            ["valid", object()],  # type: ignore[list-item]
            request=None,
            model=None,
            request_kwargs={},
        )
    with pytest.raises(HarborEmbedConfigurationError, match="unknown logical"):
        prepare_embed_request(
            config,
            ["valid"],
            request=None,
            model="unknown",
            request_kwargs={},
        )


def test_defaults_preserve_explicit_values_and_use_metadata_purpose() -> None:
    config = _config(
        defaults={
            "dimensions": 3,
            "encoding_format": "float",
            "purpose": "query",
            "normalize": True,
            "batch_size": 9,
        }
    )
    defaults = config.models["primary"].default_params
    request = HarborEmbedRequest(
        inputs=("one",),
        dimensions=2,
        normalize=False,
        metadata=HarborEmbedMetadata(embedding_purpose=EmbeddingPurpose.DOCUMENT),
    )
    applied = apply_embed_defaults(request, defaults)
    assert applied.dimensions == 2
    assert applied.encoding_format is EmbeddingEncodingFormat.FLOAT
    assert applied.purpose is EmbeddingPurpose.DOCUMENT
    assert applied.normalize is False
    assert applied.batch_size == 9


def test_litellm_parameters_merge_headers_identity_and_azure_model() -> None:
    config = _config(
        "azure_openai",
        deployment_updates={
            "model": "azure/text-embedding",
            "api_base": "https://example.openai.azure.com",
            "api_version": "2025-04-01-preview",
            "deployment_name": "embed-production",
            "headers": {"X-Deployment": "one"},
        },
    )
    deployment = config.models["primary"].deployments[0]
    request = HarborEmbedRequest(
        inputs=("one", (1, 2)),
        logical_model="primary",
        dimensions=2,
        encoding_format=EmbeddingEncodingFormat.FLOAT,
        custom_headers={"X-Request": "two"},
        metadata=HarborEmbedMetadata(user_id="metadata-user"),
    )
    inputs = litellm_inputs(request)
    parameters = build_litellm_parameters(
        deployment,
        request,
        inputs=inputs,
        timeout=4,
    )
    assert inputs == ["one", [1, 2]]
    assert parameters["model"] == "azure/embed-production"
    assert parameters["dimensions"] == 2
    assert parameters["encoding_format"] == "float"
    assert parameters["user"] == "metadata-user"
    assert parameters["extra_headers"] == {"X-Deployment": "one", "X-Request": "two"}

    override = build_litellm_parameters(
        deployment,
        request,
        inputs=inputs,
        timeout=4,
        model_override="router/private",
    )
    assert override["model"] == "router/private"

    conflicting = request.model_copy(update={"extra_params": {"model": "unsafe"}})
    with pytest.raises(HarborEmbedInvalidRequestError, match="normalized parameters"):
        build_litellm_parameters(deployment, conflicting, inputs=inputs, timeout=4)


@pytest.mark.parametrize(
    ("provider", "purpose", "expected"),
    [
        ("cohere", EmbeddingPurpose.QUERY, {"input_type": "search_query"}),
        ("voyage", EmbeddingPurpose.DOCUMENT, {"input_type": "document"}),
        ("gemini", EmbeddingPurpose.CLASSIFICATION, {"task_type": "CLASSIFICATION"}),
        ("openai", EmbeddingPurpose.QUERY, {}),
        ("openai", EmbeddingPurpose.UNSPECIFIED, {}),
    ],
)
def test_provider_purpose_parameter_mapping(
    provider: str, purpose: EmbeddingPurpose, expected: dict[str, str]
) -> None:
    config = _config(provider)
    deployment = config.models["primary"].deployments[0]
    request = HarborEmbedRequest(inputs=("one",), purpose=purpose)
    parameters = build_litellm_parameters(deployment, request, inputs=["one"], timeout=1)
    for key, value in expected.items():
        assert parameters[key] == value
    if not expected:
        assert "input_type" not in parameters and "task_type" not in parameters


def test_effective_batch_size_honors_all_limits_and_batch_capability() -> None:
    config = _config(
        deployment_updates={
            "max_batch_size": 5,
            "capabilities": {"batch": True, "max_batch_size": 4},
        },
        client_updates={"default_batch_size": 8},
    )
    deployment = config.models["primary"].deployments[0]
    request = HarborEmbedRequest(inputs=("one",), batch_size=6)
    assert effective_batch_size(config, deployment, request) == 4

    single = deployment.model_copy(
        update={"capabilities": deployment.capabilities.model_copy(update={"batch": False})}
    )
    assert effective_batch_size(config, single, request) == 1
