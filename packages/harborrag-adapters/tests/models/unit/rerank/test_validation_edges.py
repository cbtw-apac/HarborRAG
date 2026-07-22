from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.models.rerank import HarborRerankClientConfig
from harborrag_adapters.models.rerank.validation import (
    validate_rerank_configuration,
    validate_rerank_request,
)
from harborrag_core.models.errors import (
    HarborRerankCapabilityError,
    HarborRerankConfigurationError,
    HarborRerankInvalidRequestError,
)
from harborrag_core.models.rerank import HarborRerankDocument, HarborRerankRequest

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _config(
    *, capabilities: dict[str, Any] | None = None, **updates: Any
) -> HarborRerankClientConfig:
    document: dict[str, Any] = {
        "default_model": "primary",
        "models": {
            "primary": {
                "provider": "cohere",
                "model": "cohere/rerank",
                "api_key": "key",
                "capabilities": capabilities or {},
            }
        },
    }
    document.update(updates)
    return HarborRerankClientConfig.from_dict(document)


@pytest.mark.parametrize(
    "rerank_request",
    [
        HarborRerankRequest(query="abcd", documents=(HarborRerankDocument.text("a"),)),
        HarborRerankRequest(query="a", documents=(HarborRerankDocument.text("abcd"),)),
        HarborRerankRequest(
            query="a",
            documents=(HarborRerankDocument.text("a"),),
            extra_params={"a": 1, "b": 2},
        ),
        HarborRerankRequest(
            query="a",
            documents=(HarborRerankDocument.text("a"),),
            extra_params={"unsafe": True},
        ),
        HarborRerankRequest(
            query="a",
            documents=(HarborRerankDocument.text("a"),),
            custom_headers={"Authorization": "secret"},
        ),
    ],
)
def test_rerank_request_limits_and_security(
    rerank_request: HarborRerankRequest,
) -> None:
    config = _config(
        max_query_characters=2,
        max_document_characters=2,
        security={"max_extra_params": 1},
    )
    deployment = config.models["primary"].deployments[0]
    with pytest.raises(HarborRerankInvalidRequestError):
        validate_rerank_request(rerank_request, config, deployment)


@pytest.mark.parametrize(
    ("request_updates", "feature", "capabilities"),
    [
        (
            {"documents": (HarborRerankDocument(content={"text": "a"}),)},
            "structured",
            {},
        ),
        (
            {
                "documents": (HarborRerankDocument(content={"text": "a"}),),
                "rank_fields": ("text",),
            },
            "rank_fields",
            {"structured_documents": True},
        ),
        ({"return_documents": True}, "returned", {"return_documents": False}),
        ({"max_chunks_per_doc": 1}, "max_chunks", {}),
        ({"max_tokens_per_doc": 1}, "max_tokens", {}),
        ({"instruction": "rank carefully"}, "instructions", {}),
    ],
)
def test_rerank_capability_edges(
    request_updates: dict[str, Any], feature: str, capabilities: dict[str, Any]
) -> None:
    base = {"query": "query", "documents": (HarborRerankDocument.text("a"),)}
    request = HarborRerankRequest(**(base | request_updates))
    config = _config(capabilities=capabilities)
    deployment = config.models["primary"].deployments[0]
    with pytest.raises(HarborRerankCapabilityError, match=feature):
        validate_rerank_request(request, config, deployment)


def test_rerank_configuration_rejects_router_and_disabled_route() -> None:
    with pytest.raises(HarborRerankConfigurationError, match="reliable sync rerank"):
        validate_rerank_configuration(_config(routing={"engine": "litellm_router"}))

    config = _config()
    deployment = config.models["primary"].deployments[0].model_copy(update={"enabled": False})
    logical = config.models["primary"].model_copy(update={"deployments": (deployment,)})
    with pytest.raises(HarborRerankConfigurationError, match="enabled"):
        validate_rerank_configuration(config.model_copy(update={"models": {"primary": logical}}))
