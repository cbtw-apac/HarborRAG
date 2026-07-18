from __future__ import annotations

from typing import Any

import pytest
from harborrag_adapters.models.common.config import RoutingConfig, RoutingEngine
from harborrag_adapters.models.rerank.configs import (
    HarborRerankDefaults,
    HarborRerankProviderConfig,
)
from harborrag_adapters.models.rerank.normalization import (
    normalize_rerank_response,
    normalize_rerank_usage,
)
from harborrag_adapters.models.rerank.parameters import (
    apply_rerank_defaults,
    build_litellm_parameters,
    build_rerank_request,
    ensure_rerank_request_id,
    normalize_rerank_documents,
    prepare_rerank_request,
)
from harborrag_adapters.models.rerank.registry import HarborRerankProvider
from harborrag_adapters.models.rerank.validation import (
    validate_rerank_configuration,
    validate_rerank_request,
)
from harborrag_core.models.capabilities import HarborRerankCapabilities
from harborrag_core.models.errors import (
    HarborRerankCapabilityError,
    HarborRerankConfigurationError,
    HarborRerankInvalidRequestError,
    HarborRerankMalformedResponseError,
)
from harborrag_core.models.rerank import (
    HarborRerankDocument,
    HarborRerankRequest,
)
from model_runtime_support import rerank_config
from pydantic import SecretStr


def deployment(**updates: Any) -> HarborRerankProviderConfig:
    values: dict[str, Any] = {
        "name": "rerank-a",
        "provider": HarborRerankProvider.COHERE,
        "model": "cohere/rerank-test",
        "api_key": "secret",
        "headers": {"X-Deploy": SecretStr("one")},
        "max_documents": 5,
        "capabilities": HarborRerankCapabilities(max_documents=4),
    }
    values.update(updates)
    return HarborRerankProviderConfig(**values)


def request(**updates: Any) -> HarborRerankRequest:
    values: dict[str, Any] = {
        "query": "query",
        "documents": (
            HarborRerankDocument(content="first", document_id="a", metadata={"source": 1}),
            HarborRerankDocument(content={"text": "second"}, document_id="b"),
        ),
        "logical_model": "primary",
        "metadata": {"request_id": "req"},
        "return_documents": True,
    }
    values.update(updates)
    return HarborRerankRequest(**values)


def test_normalize_rerank_response_orders_scores_and_metadata() -> None:
    raw = {
        "id": "response",
        "results": [
            {
                "index": 0,
                "relevance_score": 0.2,
                "document": {"text": "provider first"},
            },
            {"index": 1, "score": 0.9, "document": {"text": "provider second"}},
        ],
        "meta": {"billed_units": {"search_units": 2}, "tokens": {"input_tokens": 3}},
        "_hidden_params": {
            "custom_llm_provider": "cohere",
            "model": "provider-model",
            "model_id": "deployment-id",
            "provider_request_id": "provider-request",
            "response_cost": 0.03,
            "cache_hit": True,
            "region_name": "us",
        },
    }
    response = normalize_rerank_response(
        raw,
        request=request(),
        logical_model="primary",
        deployment=deployment(),
        request_id="req",
        latency_ms=4,
        retry_count=1,
    )
    assert [item.index for item in response.results] == [1, 0]
    assert response.results[0].rank == 1
    assert response.results[0].document == "provider second"
    assert response.results[1].metadata == {"source": 1}
    assert response.usage.search_units == 2
    assert response.provider_model == "provider-model"
    assert response.provider_request_id == "provider-request"
    assert response.estimated_cost_usd == 0.03
    assert response.cache_hit and response.retry_count == 1


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"results": "bad"},
        {"results": [{"index": -1, "relevance_score": 1}]},
        {
            "results": [
                {"index": 0, "relevance_score": 1},
                {"index": 0, "relevance_score": 0},
            ]
        },
        {"results": [{"index": 0, "relevance_score": True}]},
        {"results": [{"index": 0, "relevance_score": float("inf")}]},
    ],
)
def test_malformed_rerank_responses_are_rejected(raw: object) -> None:
    with pytest.raises(HarborRerankMalformedResponseError):
        normalize_rerank_response(
            raw,
            request=request(),
            logical_model="primary",
            deployment=deployment(),
            request_id="req",
            latency_ms=1,
            retry_count=0,
        )


def test_rerank_top_n_and_document_return_semantics() -> None:
    with pytest.raises(HarborRerankMalformedResponseError, match="top_n"):
        normalize_rerank_response(
            {"results": [{"index": 0, "score": 1}, {"index": 1, "score": 0}]},
            request=request(top_n=1),
            logical_model="primary",
            deployment=deployment(),
            request_id="r",
            latency_ms=1,
            retry_count=0,
        )
    hidden = normalize_rerank_response(
        {"results": [{"index": 0, "score": 1, "document": "provider"}]},
        request=request(return_documents=False),
        logical_model="primary",
        deployment=deployment(),
        request_id="r",
        latency_ms=1,
        retry_count=0,
    )
    assert hidden.results[0].document is None
    fallback = normalize_rerank_response(
        {"results": [{"index": 0, "score": 1}]},
        request=request(),
        logical_model="primary",
        deployment=deployment(),
        request_id="r",
        latency_ms=1,
        retry_count=0,
    )
    assert fallback.results[0].document == "first"


def test_rerank_usage_handles_nested_and_invalid_values() -> None:
    usage = normalize_rerank_usage(
        {
            "billed_units": {"total_tokens": 5},
            "tokens": {"input_tokens": 2, "output_tokens": 1},
        }
    )
    assert usage.total_tokens == 5 and usage.input_tokens == 2
    fallback = normalize_rerank_usage({"input_tokens": 2, "output_tokens": 3, "search_units": -1})
    assert fallback.total_tokens == 5 and fallback.search_units == 0


def test_document_and_request_builders_validate_input_styles() -> None:
    typed = HarborRerankDocument.text("typed")
    documents = normalize_rerank_documents(["text", {"title": "structured"}, typed])
    assert documents[0].content == "text" and documents[1].content == {"title": "structured"}
    assert documents[2] is typed
    with pytest.raises(TypeError):
        normalize_rerank_documents([object()])
    built = build_rerank_request(
        "query",
        ["a"],
        request=None,
        model="primary",
        request_kwargs={"metadata": {"tenant_id": "t"}},
    )
    assert built.logical_model == "primary" and built.metadata.tenant_id == "t"
    existing = request(logical_model="old")
    assert (
        build_rerank_request(
            None, None, request=existing, model="new", request_kwargs={}
        ).logical_model
        == "new"
    )
    with pytest.raises(HarborRerankInvalidRequestError):
        build_rerank_request("q", ["a"], request=existing, model=None, request_kwargs={})
    with pytest.raises(HarborRerankInvalidRequestError):
        build_rerank_request(None, None, request=None, model=None, request_kwargs={})


def test_defaults_identity_and_request_preparation() -> None:
    defaults = HarborRerankDefaults(
        top_n=10,
        return_documents=False,
        max_chunks_per_doc=2,
        max_tokens_per_doc=20,
        instruction="rank",
    )
    defaulted = apply_rerank_defaults(request(return_documents=None), defaults)
    assert defaulted.top_n == 2 and defaulted.return_documents is False
    assert defaulted.max_chunks_per_doc == 2 and defaulted.instruction == "rank"
    identified = ensure_rerank_request_id(
        HarborRerankRequest(query="q", documents=(HarborRerankDocument.text("a"),))
    )
    assert identified.metadata.request_id and ensure_rerank_request_id(identified) is identified
    logical, selected, prepared = prepare_rerank_request(
        rerank_config(), "q", ["a"], request=None, model=None, request_kwargs={}
    )
    assert logical == "primary" and selected.name == "rerank-a" and prepared.metadata.request_id
    with pytest.raises(HarborRerankConfigurationError):
        prepare_rerank_request(
            rerank_config(),
            "q",
            ["a"],
            request=None,
            model="missing",
            request_kwargs={},
        )


def test_litellm_rerank_parameters_include_complete_candidate_set() -> None:
    item = request(
        documents=(
            HarborRerankDocument(content={"title": "first"}),
            HarborRerankDocument(content={"title": "second"}),
        ),
        top_n=2,
        rank_fields=("title",),
        max_chunks_per_doc=2,
        max_tokens_per_doc=10,
        instruction="rank carefully",
        custom_headers={"X-Request": SecretStr("two")},
        extra_params={"return_original_response": True},
    )
    params = build_litellm_parameters(deployment(), item, timeout=5, litellm_provider="cohere")
    assert params["documents"] == [{"title": "first"}, {"title": "second"}]
    assert params["rank_fields"] == ["title"]
    assert params["extra_headers"] == {"X-Deploy": "one", "X-Request": "two"}
    assert params["return_original_response"] is True
    with pytest.raises(HarborRerankInvalidRequestError):
        build_litellm_parameters(
            deployment(),
            item.model_copy(update={"extra_params": {"model": "bad"}}),
            timeout=1,
        )


def test_rerank_request_capabilities_limits_and_security() -> None:
    limited = deployment(
        max_documents=1,
        capabilities=HarborRerankCapabilities(
            max_documents=1,
            structured_documents=False,
            rank_fields=False,
            return_documents=False,
            max_chunks_per_doc=False,
            max_tokens_per_doc=False,
            instruction=False,
        ),
    )
    config = rerank_config(deployments=(limited,))
    invalid = [
        request(),
        request(
            documents=(HarborRerankDocument(content={"title": "a"}),),
            rank_fields=("title",),
        ),
        request(documents=(HarborRerankDocument.text("a"),), return_documents=True),
        request(documents=(HarborRerankDocument.text("a"),), max_chunks_per_doc=1),
        request(documents=(HarborRerankDocument.text("a"),), max_tokens_per_doc=1),
        request(documents=(HarborRerankDocument.text("a"),), instruction="rank"),
        request(documents=(HarborRerankDocument.text("a"),), extra_params={"model": "bad"}),
        request(
            documents=(HarborRerankDocument.text("a"),),
            custom_headers={"Authorization": "bad"},
        ),
    ]
    for item in invalid:
        with pytest.raises((HarborRerankCapabilityError, HarborRerankInvalidRequestError)):
            validate_rerank_request(item, config, limited)
    tiny = config.model_copy(update={"max_query_characters": 1, "max_document_characters": 1})
    with pytest.raises(HarborRerankInvalidRequestError):
        validate_rerank_request(
            request(query="too long", documents=(HarborRerankDocument.text("a"),)),
            tiny,
            limited,
        )
    with pytest.raises(HarborRerankInvalidRequestError):
        validate_rerank_request(
            request(query="q", documents=(HarborRerankDocument.text("long"),)),
            tiny,
            limited,
        )


def test_litellm_router_is_rejected_for_reranking() -> None:
    config = rerank_config().model_copy(
        update={"routing": RoutingConfig(engine=RoutingEngine.LITELLM_ROUTER)}
    )
    with pytest.raises(HarborRerankConfigurationError):
        validate_rerank_configuration(config)
