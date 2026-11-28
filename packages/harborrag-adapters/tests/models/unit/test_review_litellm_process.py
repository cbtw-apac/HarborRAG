from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import litellm
import pytest
from litellm.llms.openai.openai import OpenAIChatCompletion
from model_invocation_support import (
    FakeEmbeddingInvocation,
    FakeRerankInvocation,
    embedding_response,
    rerank_response,
)
from model_runtime_support import embed_config, rerank_config

from harborrag_adapters.models.chat.backends import LiteLLMDirectBackend
from harborrag_adapters.models.embed import (
    EmbedProviderRegistry,
    HarborEmbedClient,
    HarborEmbedProvider,
    HarborEmbedProviderConfig,
)
from harborrag_adapters.models.embed.invocation import LiteLLMEmbeddingInvocation
from harborrag_adapters.models.rerank import HarborRerankingClient
from harborrag_adapters.models.rerank.invocation import LiteLLMRerankInvocation
from harborrag_adapters.models.runtime.config import ConnectionPoolConfig, RetryPolicyConfig
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_adapters.models.runtime.litellm_backend import configure_litellm
from harborrag_adapters.models.runtime.litellm_router import build_litellm_router
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.errors import HarborEmbedError

pytestmark = pytest.mark.unit


@pytest.fixture
def unconfigured_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo every process-wide LiteLLM setting for the duration of one test."""
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP")
    monkeypatch.setattr(litellm, "suppress_debug_info", False)
    monkeypatch.setattr(litellm, "redact_messages_in_exceptions", False)
    monkeypatch.setattr(litellm, "DEFAULT_MAX_RETRIES", 2)


def _assert_configured() -> None:
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"
    assert litellm.suppress_debug_info is True
    assert litellm.redact_messages_in_exceptions is True
    assert litellm.DEFAULT_MAX_RETRIES == 0


@pytest.mark.usefixtures("unconfigured_litellm")
def test_configure_litellm_sets_local_cost_map_quiet_errors_and_no_sdk_retries() -> None:
    configure_litellm()

    _assert_configured()


@pytest.mark.usefixtures("unconfigured_litellm")
def test_configure_litellm_keeps_an_operator_cost_map_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "false")

    configure_litellm()

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "false"


def _direct_chat_backend() -> object:
    return LiteLLMDirectBackend(
        connections=SharedConnectionLifecycle(ConnectionPoolConfig(enabled=False))
    )


def _router() -> object:
    return build_litellm_router(
        embed_config(),
        embed_config().models,
        provider_resolver=lambda deployment: EmbedProviderRegistry.default().get(
            deployment.provider
        ),
        router_factory=lambda **kwargs: kwargs,
    )


@pytest.mark.usefixtures("unconfigured_litellm")
@pytest.mark.parametrize(
    "build",
    [LiteLLMEmbeddingInvocation, LiteLLMRerankInvocation, _direct_chat_backend, _router],
    ids=["embed", "rerank", "chat", "router"],
)
def test_default_litellm_entrypoints_configure_litellm(build: Callable[[], object]) -> None:
    build()

    _assert_configured()


def test_embed_call_turns_off_sdk_retries() -> None:
    invocation = FakeEmbeddingInvocation([embedding_response([[1.0, 0.0, 0.0]])])

    HarborEmbedClient(embed_config(), invocation=invocation).embed("synthetic text")

    assert invocation.calls[0]["max_retries"] == 0


def test_rerank_call_turns_off_sdk_retries() -> None:
    invocation = FakeRerankInvocation([rerank_response([(0, 0.9)])])

    HarborRerankingClient(rerank_config(), invocation=invocation).rerank(
        "synthetic question", ["synthetic passage"]
    )

    assert invocation.calls[0]["max_retries"] == 0


def test_router_deployments_turn_off_sdk_retries() -> None:
    router: Any = _router()

    assert {item["litellm_params"]["max_retries"] for item in router["model_list"]} == {0}


@pytest.mark.usefixtures("unconfigured_litellm")
def test_openai_compatible_embedding_reaches_the_sdk_client_without_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_retries: list[object] = []

    def refuse_client(_self: object, **kwargs: Any) -> object:
        client_retries.append(kwargs.get("max_retries"))
        raise ConnectionError("unit tests open no connections")

    monkeypatch.setattr(OpenAIChatCompletion, "_get_openai_client", refuse_client)
    deployment = HarborEmbedProviderConfig(
        name="gateway-embed",
        provider=HarborEmbedProvider.OPENAI_COMPATIBLE,
        model="text-embedding-test",
        api_base="http://127.0.0.1:9/v1",
        api_key="synthetic-key",
        expected_dimensions=3,
        capabilities=HarborEmbedCapabilities(batch=True, default_dimensions=3),
    )
    config = embed_config(
        deployments=(deployment,),
        retry=RetryPolicyConfig(
            same_deployment_attempts=1, base_delay_seconds=0, max_delay_seconds=0
        ),
    )

    with pytest.raises(HarborEmbedError):
        HarborEmbedClient(config).embed("synthetic text")

    assert client_retries == [0]
