from __future__ import annotations

import pytest
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.rerank import (
    HarborRerankClientConfig,
    HarborRerankingClient,
)
from harborrag_core.models.errors import (
    HarborRerankCapabilityError,
    HarborRerankInvalidRequestError,
    HarborRerankMalformedResponseError,
    HarborRerankProviderError,
    HarborRerankTimeoutError,
)
from harborrag_core.models.rerank import HarborRerankDocument
from model_invocation_support import FakeRerankInvocation, rerank_response
from pydantic import ValidationError


def rerank_config(
    *,
    attempts: int = 1,
    capabilities: dict[str, object] | None = None,
) -> HarborRerankClientConfig:
    return HarborRerankClientConfig.from_dict(
        {
            "default_model": "primary",
            "retry": {
                "same_deployment_attempts": attempts,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
            },
            "timeouts": {"request_seconds": 13},
            "models": {
                "primary": {
                    "provider": "cohere",
                    "model": "cohere/rerank-v3.5",
                    "api_key": "key",
                    "capabilities": capabilities or {},
                }
            },
        }
    )


def test_sync_reranking_preserves_source_identity_and_metadata() -> None:
    documents = [
        HarborRerankDocument.text("first", document_id="first-id", metadata={"source": "one"}),
        HarborRerankDocument.text("second", document_id="second-id", metadata={"source": "two"}),
    ]
    invocation = FakeRerankInvocation([rerank_response([(0, 0.2), (1, 0.9)], search_units=1)])

    response = HarborRerankingClient(rerank_config(), invocation=invocation).rerank(
        "query", documents
    )

    assert response.indices == (1, 0)
    assert response.results[0].document_id == "second-id"
    assert response.results[0].metadata == {"source": "two"}
    assert response.results[0].document == "second"
    assert response.usage.search_units == 1
    assert invocation.calls[0]["documents"] == ["first", "second"]
    assert invocation.calls[0]["timeout"] == 13


@pytest.mark.asyncio
async def test_async_reranking() -> None:
    invocation = FakeRerankInvocation([rerank_response([(0, 0.8), (1, 0.1)])])
    client = HarborRerankingClient(rerank_config(), invocation=invocation)

    response = await client.arerank("query", ["first", "second"])

    assert response.indices == (0, 1)
    assert invocation.async_calls[0]["query"] == "query"


def test_top_n_is_forwarded_and_limits_results() -> None:
    invocation = FakeRerankInvocation([rerank_response([(2, 0.9), (0, 0.7)])])

    response = HarborRerankingClient(rerank_config(), invocation=invocation).rerank(
        "query", ["zero", "one", "two"], top_n=2
    )

    assert response.indices == (2, 0)
    assert invocation.calls[0]["top_n"] == 2


def test_tied_scores_use_stable_source_index_order() -> None:
    invocation = FakeRerankInvocation([rerank_response([(2, 0.5), (0, 0.5), (1, 0.5)])])

    response = HarborRerankingClient(rerank_config(), invocation=invocation).rerank(
        "query", ["zero", "one", "two"]
    )

    assert response.indices == (0, 1, 2)
    assert tuple(item.rank for item in response.results) == (1, 2, 3)


@pytest.mark.parametrize(
    ("query", "documents", "top_n"),
    [
        ("query", [], None),
        (" ", ["document"], None),
        ("query", ["document"], 0),
        ("query", ["document"], 2),
    ],
)
def test_invalid_requests_are_rejected_before_invocation(
    query: str,
    documents: list[str],
    top_n: int | None,
) -> None:
    invocation = FakeRerankInvocation()
    client = HarborRerankingClient(rerank_config(), invocation=invocation)

    with pytest.raises(HarborRerankInvalidRequestError, match="invalid rerank request"):
        client.rerank(query, documents, top_n=top_n)

    assert invocation.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"results": [{"index": 4, "relevance_score": 0.5}]},
        {"results": [{"index": 0, "relevance_score": float("nan")}]},
        {
            "results": [
                {"index": 0, "relevance_score": 0.8},
                {"index": 0, "relevance_score": 0.7},
            ]
        },
        {"unexpected": []},
    ],
)
def test_malformed_provider_responses_are_rejected(response: object) -> None:
    client = HarborRerankingClient(rerank_config(), invocation=FakeRerankInvocation([response]))

    with pytest.raises(HarborRerankMalformedResponseError):
        client.rerank("query", ["document"])


def test_timeout_is_retried_and_remains_typed_when_exhausted() -> None:
    invocation = FakeRerankInvocation([TimeoutError("slow"), TimeoutError("still slow")])

    with pytest.raises(HarborRerankTimeoutError):
        HarborRerankingClient(rerank_config(attempts=2), invocation=invocation).rerank(
            "query", ["document"]
        )

    assert len(invocation.calls) == 2


def test_provider_error_is_normalized() -> None:
    client = HarborRerankingClient(
        rerank_config(), invocation=FakeRerankInvocation([RuntimeError("provider failed")])
    )

    with pytest.raises(HarborRerankProviderError) as captured:
        client.rerank("query", ["document"])

    assert captured.value.provider == "cohere"
    assert captured.value.request_id


def test_structured_documents_require_declared_capability() -> None:
    invocation = FakeRerankInvocation()

    with pytest.raises(HarborRerankCapabilityError, match="structured rerank"):
        HarborRerankingClient(rerank_config(), invocation=invocation).rerank(
            "query", [{"title": "document"}]
        )

    assert invocation.calls == []


def test_unknown_provider_is_rejected_during_configuration() -> None:
    document = {
        "default_model": "primary",
        "models": {
            "primary": {
                "provider": "unknown-provider",
                "model": "unknown/model",
            }
        },
    }

    with pytest.raises(ValidationError):
        HarborRerankClientConfig.from_dict(document)


@pytest.mark.asyncio
async def test_lifecycle_closes_owned_invocation_once() -> None:
    sync_invocation = FakeRerankInvocation()
    sync_client = HarborRerankingClient(rerank_config(), invocation=sync_invocation)
    sync_client.close()
    sync_client.close()
    assert sync_invocation.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        sync_client.rerank("query", ["document"])

    async_invocation = FakeRerankInvocation()
    async_client = HarborRerankingClient(rerank_config(), invocation=async_invocation)
    await async_client.aclose()
    await async_client.aclose()
    assert async_invocation.aclose_count == 1


def test_borrowed_invocation_is_not_closed() -> None:
    invocation = FakeRerankInvocation()
    client = HarborRerankingClient(
        rerank_config(),
        invocation=invocation,
        resource_ownership=ResourceOwnership.BORROWED,
    )

    client.close()

    assert invocation.close_count == 0
