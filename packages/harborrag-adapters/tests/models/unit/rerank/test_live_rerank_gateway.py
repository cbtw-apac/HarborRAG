from __future__ import annotations

import json

import httpx
import pytest

from harborrag_adapters.models.rerank import HarborRerankClientConfig, HarborRerankingClient
from harborrag_core.models.errors import HarborRerankConfigurationError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.graybox,
    pytest.mark.filterwarnings("ignore:Item 'summary' on TypedDict class:UserWarning"),
]

GATEWAY_URL = "https://gateway.example.com"
SHIPPED_DEFAULT_BASE = "https://api.openai.com/v1"
AMBIENT_KEY_VARIABLES = ("COHERE_API_KEY", "CO_API_KEY", "LITELLM_PROXY_API_KEY")


class _GatewayRecorder:
    """Answer every LiteLLM request in place of the gateway and keep it for assertions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def send(self, request: httpx.Request, **_: object) -> httpx.Response:
        self.requests.append(request)
        documents = json.loads(request.content).get("documents") or []
        results = [{"index": index, "relevance_score": 1.0} for index in range(len(documents))]
        return httpx.Response(
            200,
            json={"id": "stub-rerank", "results": results, "meta": {}},
            request=request,
        )


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> _GatewayRecorder:
    recorder = _GatewayRecorder()
    monkeypatch.setattr(httpx.Client, "send", recorder.send)
    return recorder


def _client(*, provider: str, api_base: str, api_key: str) -> HarborRerankingClient:
    config = HarborRerankClientConfig.from_dict(
        {
            "rerank": {
                "default_model": "primary",
                "retry": {"same_deployment_attempts": 1},
                "models": {
                    "primary": {
                        "deployments": [
                            {
                                "name": "gateway-rerank",
                                "provider": provider,
                                "model": "rerank-test",
                                "api_base": api_base,
                                "api_key": api_key,
                            }
                        ]
                    }
                },
            }
        }
    )
    return HarborRerankingClient.from_config(config)


@pytest.mark.parametrize("provider", ["litellm_proxy", "cohere"])
@pytest.mark.parametrize("blank", ["", "  ", "\r\n"])
def test_rerank_blank_key_with_ambient_keys_set_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, gateway: _GatewayRecorder, provider: str, blank: str
) -> None:
    for variable in AMBIENT_KEY_VARIABLES:
        monkeypatch.setenv(variable, f"ambient-{variable.lower()}")

    with pytest.raises(HarborRerankConfigurationError, match="empty api_key") as refused:
        _client(provider=provider, api_base=GATEWAY_URL, api_key=blank).rerank(
            "deploy runbook", ["first", "second"]
        )

    assert "ambient-" not in str(refused.value)
    assert gateway.requests == []


@pytest.mark.parametrize(
    ("api_base", "expected_url"),
    [
        (GATEWAY_URL, f"{GATEWAY_URL}/v1/rerank"),
        (f"{GATEWAY_URL}/", f"{GATEWAY_URL}/v1/rerank"),
        (f"{GATEWAY_URL}/v1", f"{GATEWAY_URL}/v1/rerank"),
        (f"{GATEWAY_URL}/v1/", f"{GATEWAY_URL}/v1/rerank"),
        (f"{GATEWAY_URL}/proxy/v1", f"{GATEWAY_URL}/proxy/v1/rerank"),
        (f"{GATEWAY_URL}/v1/rerank", f"{GATEWAY_URL}/v1/rerank"),
        (SHIPPED_DEFAULT_BASE, "https://api.openai.com/v1/rerank"),
    ],
)
def test_litellm_proxy_rerank_posts_once_to_v1_rerank_whatever_the_base_ends_with(
    gateway: _GatewayRecorder, api_base: str, expected_url: str
) -> None:
    client = _client(provider="litellm_proxy", api_base=api_base, api_key="gateway-key")

    client.rerank("deploy runbook", ["first", "second"])

    assert [str(request.url) for request in gateway.requests] == [expected_url]


def test_cohere_rerank_base_ending_in_v1_keeps_one_version_segment(
    gateway: _GatewayRecorder,
) -> None:
    client = _client(provider="cohere", api_base=f"{GATEWAY_URL}/v1", api_key="cohere-key")

    client.rerank("deploy runbook", ["first", "second"])

    assert [str(request.url) for request in gateway.requests] == [f"{GATEWAY_URL}/v2/rerank"]
