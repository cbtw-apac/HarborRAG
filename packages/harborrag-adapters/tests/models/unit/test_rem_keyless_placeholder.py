from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    HarborChatClient,
    HarborChatClientConfig,
)
from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.models.rerank import HarborRerankClientConfig, HarborRerankingClient
from harborrag_adapters.models.runtime.litellm_backend import NO_API_KEY_PLACEHOLDER

pytestmark = [
    pytest.mark.unit,
    pytest.mark.graybox,
    pytest.mark.filterwarnings("ignore:Items? .* on TypedDict class:UserWarning"),
]

AMBIENT_PREFIX = "demo-ambient-"
AMBIENT_KEY_VARIABLES = (
    "OPENAI_API_KEY",
    "HOSTED_VLLM_API_KEY",
    "INFINITY_API_KEY",
    "OLLAMA_API_KEY",
    "LITELLM_PROXY_API_KEY",
    "COHERE_API_KEY",
    "CO_API_KEY",
)
AMBIENT_BASE_VARIABLES = (
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "HOSTED_VLLM_API_BASE",
    "INFINITY_API_BASE",
    "OLLAMA_API_BASE",
    "LITELLM_PROXY_API_BASE",
    "COHERE_API_BASE",
)
REAL_KEY = "demo-real-key-0000"
PROXY_KEY = "demo-proxy-key-0000"
HEADER_KEY = "Bearer demo-header-key-0000"
EMBEDDING = [0.25, 0.5, 0.75]
OLLAMA_MODEL_PROBE = "POST /api/show "
"""LiteLLM's Ollama route asks about the model before it calls it.

One such request goes to the deployment's ``api_base`` with no key. Another goes to
``OLLAMA_API_BASE`` with ``OLLAMA_API_KEY``, whatever the deployment sets: HarborRAG
cannot pass that request a key, so it is the one request allowed on the environment's
base.
"""
KEYLESS_EMBED_PROVIDERS = frozenset({"ollama"})
"""Providers whose LiteLLM embedding handler sends no key, the deployment's or any other."""


@dataclass(frozen=True, slots=True)
class _Recorded:
    line: str
    headers: tuple[tuple[str, str], ...]
    body: str

    def text(self) -> str:
        rendered = "\n".join(f"{name}: {value}" for name, value in self.headers)
        return f"{self.line}\n{rendered}\n{self.body}"

    def carries(self, value: str) -> bool:
        return any(value in header for _, header in self.headers)


@dataclass
class _Endpoint:
    url: str
    requests: list[_Recorded] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Wire:
    """The deployment's ``api_base`` and the base every ``*_API_BASE`` variable names."""

    gateway: _Endpoint
    ambient: _Endpoint


class _RecordingServer(ThreadingHTTPServer):
    endpoint: _Endpoint


class _StubHandler(BaseHTTPRequestHandler):
    server: _RecordingServer

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        self.server.endpoint.requests.append(
            _Recorded(line=self.requestline, headers=tuple(self.headers.items()), body=body)
        )
        request = json.loads(body) if body.startswith("{") else {}
        if self.path.endswith("/chat/completions") and request.get("stream"):
            self._send(_chat_stream(), content_type="text/event-stream")
            return
        self._send(json.dumps(_response_for(self.path, request)).encode("utf-8"))

    def _send(self, payload: bytes, *, content_type: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _response_for(path: str, request: dict[str, Any]) -> dict[str, Any]:
    if path.endswith("/chat/completions"):
        return {
            "id": "stub-chat",
            "object": "chat.completion",
            "created": 0,
            "model": "demo-chat",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    if path.endswith("/embeddings"):
        inputs = request.get("input") or [""]
        count = len(inputs) if isinstance(inputs, list) else 1
        return {
            "object": "list",
            "model": "demo-embed",
            "data": [
                {"object": "embedding", "index": index, "embedding": EMBEDDING}
                for index in range(count)
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
    if path.endswith("/api/embed"):
        inputs = request.get("input") or [""]
        return {
            "model": "demo-embed",
            "embeddings": [EMBEDDING for _ in inputs],
            "prompt_eval_count": 1,
        }
    if path.endswith("/api/generate"):
        return {
            "model": "demo-chat",
            "created_at": "2026-01-01T00:00:00Z",
            "response": "ok",
            "done": True,
            "prompt_eval_count": 1,
            "eval_count": 1,
        }
    if path.endswith("/rerank"):
        documents = request.get("documents") or []
        return {
            "id": "stub-rerank",
            "results": [
                {"index": index, "relevance_score": 1.0} for index in range(len(documents))
            ],
            "meta": {},
            "usage": {"total_tokens": 1},
        }
    return {}


def _chat_stream() -> bytes:
    chunks = [
        {
            "id": "stub-chat",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "demo-chat",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}],
        },
        {
            "id": "stub-chat",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "demo-chat",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    events = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return f"{events}data: [DONE]\n\n".encode()


def _serve() -> tuple[_RecordingServer, threading.Thread]:
    server = _RecordingServer(("127.0.0.1", 0), _StubHandler)
    server.endpoint = _Endpoint(url=f"http://127.0.0.1:{server.server_port}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Wire]:
    servers = [_serve()[0], _serve()[0]]
    gateway, ambient = (server.endpoint for server in servers)
    for variable in AMBIENT_KEY_VARIABLES:
        monkeypatch.setenv(variable, f"{AMBIENT_PREFIX}{variable.lower()}")
    for variable in AMBIENT_BASE_VARIABLES:
        monkeypatch.setenv(variable, ambient.url)
    yield _Wire(gateway=gateway, ambient=ambient)
    for server in servers:
        server.shutdown()
        server.server_close()


def _deployment(provider: str, model: str, wire: _Wire, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": f"{provider}-deployment",
        "provider": provider,
        "model": model,
        "api_base": wire.gateway.url,
        **fields,
    }


def _chat_config(
    provider: str, wire: _Wire, fields: dict[str, Any], *, router: bool = False
) -> HarborChatClientConfig:
    routing = (
        {"backend": {"type": "litellm_router"}, "routing": {"engine": "litellm_router"}}
        if router
        else {}
    )
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            **routing,
            "retry": {"same_deployment_attempts": 1},
            "security": {"allow_custom_providers": True},
            "models": {
                "primary": {"deployments": [_deployment(provider, "demo-chat", wire, fields)]}
            },
        }
    )


def _embed_config(
    provider: str, wire: _Wire, fields: dict[str, Any], *, router: bool = False
) -> HarborEmbedClientConfig:
    routing = {"routing": {"engine": "litellm_router"}} if router else {}
    return HarborEmbedClientConfig.from_dict(
        {
            "embed": {
                "default_model": "primary",
                **routing,
                "retry": {"same_deployment_attempts": 1},
                "security": {"allow_custom_providers": True},
                "models": {
                    "primary": {
                        "embedding_space": "demo-space",
                        "deployments": [_deployment(provider, "demo-embed", wire, fields)],
                    }
                },
            }
        }
    )


def _rerank_config(provider: str, wire: _Wire, fields: dict[str, Any]) -> HarborRerankClientConfig:
    return HarborRerankClientConfig.from_dict(
        {
            "rerank": {
                "default_model": "primary",
                "retry": {"same_deployment_attempts": 1},
                "security": {"allow_custom_providers": True},
                "models": {
                    "primary": {"deployments": [_deployment(provider, "demo-rerank", wire, fields)]}
                },
            }
        }
    )


CHAT_CASES = [
    pytest.param("openai_compatible", {}, id="openai_compatible"),
    pytest.param("vllm", {}, id="vllm"),
    pytest.param("ollama", {}, id="ollama"),
    pytest.param("custom", {"custom_llm_provider": "openai"}, id="custom-openai"),
]
EMBED_CASES = [
    pytest.param("openai_compatible", {}, id="openai_compatible"),
    pytest.param(
        "openai_compatible",
        {"custom_llm_provider": "litellm_proxy"},
        id="openai_compatible-via-litellm_proxy",
    ),
    pytest.param("litellm_proxy", {}, id="litellm_proxy"),
    pytest.param("vllm", {}, id="vllm"),
    pytest.param("infinity", {}, id="infinity"),
    pytest.param("ollama", {}, id="ollama"),
    pytest.param("custom", {"custom_llm_provider": "openai"}, id="custom-openai"),
]
RERANK_CASES = [
    pytest.param("litellm_proxy", {}, id="litellm_proxy"),
    pytest.param("vllm", {}, id="vllm"),
    pytest.param("infinity", {}, id="infinity"),
    pytest.param("custom", {"custom_llm_provider": "cohere"}, id="custom-cohere"),
]
HEADER_CHAT_CASES = [
    pytest.param("openai_compatible", {}, id="openai_compatible"),
    pytest.param("ollama", {}, id="ollama"),
    pytest.param("custom", {"custom_llm_provider": "openai"}, id="custom-openai"),
]
"""Chat routes that send a configured ``Authorization`` header; vLLM's replaces it."""
HEADER_EMBED_CASES = [
    pytest.param("openai_compatible", {}, id="openai_compatible"),
    pytest.param(
        "openai_compatible",
        {"custom_llm_provider": "litellm_proxy"},
        id="openai_compatible-via-litellm_proxy",
    ),
    pytest.param("litellm_proxy", {}, id="litellm_proxy"),
    pytest.param("vllm", {}, id="vllm"),
    pytest.param("custom", {"custom_llm_provider": "openai"}, id="custom-openai"),
]
"""Embedding routes that send a configured ``Authorization`` header; Infinity's replaces it."""
MESSAGES = [{"role": "user", "content": "deploy runbook"}]


def _run_chat(config: HarborChatClientConfig, *, stream: bool) -> None:
    with HarborChatClient(config) as client:
        client.chat(MESSAGES)
        if stream:
            list(client.stream(MESSAGES))

    async def _async_chat() -> None:
        async with AsyncHarborChatClient(config) as client:
            await client.achat(MESSAGES)

    asyncio.run(_async_chat())


def _run_embed(config: HarborEmbedClientConfig) -> None:
    with HarborEmbedClient.from_config(config) as client:
        client.embed(["deploy runbook"])

    async def _async_embed() -> None:
        async with HarborEmbedClient.from_config(config) as client:
            await client.aembed(["deploy runbook"])

    asyncio.run(_async_embed())


def _run_rerank(config: HarborRerankClientConfig) -> None:
    with HarborRerankingClient.from_config(config) as client:
        client.rerank("deploy runbook", ["first", "second"])

    async def _async_rerank() -> None:
        async with HarborRerankingClient.from_config(config) as client:
            await client.arerank("deploy runbook", ["first", "second"])

    asyncio.run(_async_rerank())


def _is_ollama_probe(request: _Recorded) -> bool:
    return request.line.startswith(OLLAMA_MODEL_PROBE)


def _assert_sent(
    wire: _Wire,
    *,
    key: str | None,
    absent: tuple[str, ...] = (),
    ollama_probe: bool = False,
) -> None:
    calls = [request for request in wire.gateway.requests if not _is_ollama_probe(request)]
    assert calls, "no model call reached the deployment's api_base"
    for request in wire.gateway.requests:
        text = request.text()
        assert AMBIENT_PREFIX not in text, request.line
        for value in absent:
            assert value not in text, request.line
    for request in calls:
        if key is not None:
            assert request.carries(key), request.line
    ambient = [
        request.line
        for request in wire.ambient.requests
        if not (ollama_probe and _is_ollama_probe(request))
    ]
    assert ambient == []


@pytest.mark.parametrize("router", [False, True], ids=["direct", "router"])
@pytest.mark.parametrize(("provider", "fields"), CHAT_CASES)
def test_keyless_chat_deployment_sends_the_placeholder_and_no_ambient_key(
    wire: _Wire, provider: str, fields: dict[str, Any], router: bool
) -> None:
    _run_chat(_chat_config(provider, wire, fields, router=router), stream=provider != "ollama")

    _assert_sent(wire, key=NO_API_KEY_PLACEHOLDER, ollama_probe=provider == "ollama")


@pytest.mark.parametrize("router", [False, True], ids=["direct", "router"])
@pytest.mark.parametrize(("provider", "fields"), EMBED_CASES)
def test_keyless_embed_deployment_sends_the_placeholder_and_no_ambient_key(
    wire: _Wire, provider: str, fields: dict[str, Any], router: bool
) -> None:
    _run_embed(_embed_config(provider, wire, fields, router=router))

    keyless = provider in KEYLESS_EMBED_PROVIDERS
    _assert_sent(
        wire, key=None if keyless else NO_API_KEY_PLACEHOLDER, ollama_probe=provider == "ollama"
    )


@pytest.mark.parametrize(("provider", "fields"), RERANK_CASES)
def test_keyless_rerank_deployment_sends_the_placeholder_and_no_ambient_key(
    wire: _Wire, provider: str, fields: dict[str, Any]
) -> None:
    _run_rerank(_rerank_config(provider, wire, fields))

    _assert_sent(wire, key=NO_API_KEY_PLACEHOLDER)


@pytest.mark.parametrize(("provider", "fields"), CHAT_CASES)
def test_chat_deployment_with_a_key_sends_that_key(
    wire: _Wire, provider: str, fields: dict[str, Any]
) -> None:
    config = _chat_config(provider, wire, {"api_key": REAL_KEY, **fields})

    _run_chat(config, stream=provider != "ollama")

    _assert_sent(
        wire, key=REAL_KEY, absent=(NO_API_KEY_PLACEHOLDER,), ollama_probe=provider == "ollama"
    )


@pytest.mark.parametrize(("provider", "fields"), EMBED_CASES)
def test_embed_deployment_with_a_key_sends_that_key(
    wire: _Wire, provider: str, fields: dict[str, Any]
) -> None:
    _run_embed(_embed_config(provider, wire, {"api_key": REAL_KEY, **fields}))

    keyless = provider in KEYLESS_EMBED_PROVIDERS
    _assert_sent(
        wire,
        key=None if keyless else REAL_KEY,
        absent=(NO_API_KEY_PLACEHOLDER,),
        ollama_probe=provider == "ollama",
    )


@pytest.mark.parametrize(("provider", "fields"), RERANK_CASES)
def test_rerank_deployment_with_a_key_sends_that_key(
    wire: _Wire, provider: str, fields: dict[str, Any]
) -> None:
    _run_rerank(_rerank_config(provider, wire, {"api_key": REAL_KEY, **fields}))

    _assert_sent(wire, key=REAL_KEY, absent=(NO_API_KEY_PLACEHOLDER,))


@pytest.mark.parametrize(("provider", "fields"), HEADER_CHAT_CASES)
def test_keyless_chat_deployment_keeps_its_configured_authorization_header(
    wire: _Wire, provider: str, fields: dict[str, Any]
) -> None:
    config = _chat_config(provider, wire, {"headers": {"Authorization": HEADER_KEY}, **fields})

    _run_chat(config, stream=provider != "ollama")

    _assert_sent(
        wire, key=HEADER_KEY, absent=(NO_API_KEY_PLACEHOLDER,), ollama_probe=provider == "ollama"
    )


@pytest.mark.parametrize(("provider", "fields"), HEADER_EMBED_CASES)
def test_keyless_embed_deployment_keeps_its_configured_authorization_header(
    wire: _Wire, provider: str, fields: dict[str, Any]
) -> None:
    _run_embed(_embed_config(provider, wire, {"headers": {"Authorization": HEADER_KEY}, **fields}))

    _assert_sent(wire, key=HEADER_KEY, absent=(NO_API_KEY_PLACEHOLDER,))


@pytest.mark.parametrize("auth_mode", ["bearer", "x_litellm_api_key"])
def test_litellm_proxy_chat_backend_sends_its_own_key_and_no_ambient_key(
    wire: _Wire, auth_mode: str
) -> None:
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "backend": {
                "type": "litellm_proxy",
                "proxy": {
                    "api_base": wire.gateway.url,
                    "api_key": PROXY_KEY,
                    "auth_mode": auth_mode,
                },
            },
            "retry": {"same_deployment_attempts": 1},
            "models": {
                "primary": {
                    "deployments": [
                        {"name": "proxy-chat", "provider": "litellm_proxy", "model": "demo-chat"}
                    ]
                }
            },
        }
    )

    _run_chat(config, stream=True)

    _assert_sent(wire, key=PROXY_KEY)


@pytest.mark.xfail(
    strict=True,
    reason="LiteLLM's Ollama model probe sends OLLAMA_API_KEY to OLLAMA_API_BASE; "
    "HarborRAG has no parameter that reaches it",
)
@pytest.mark.parametrize("family", ["chat", "embed"])
def test_keyless_ollama_deployment_sends_nothing_to_the_environments_base(
    wire: _Wire, family: str
) -> None:
    unprobed_model = {"model": f"demo-{family}-{uuid4().hex}"}
    if family == "chat":
        _run_chat(_chat_config("ollama", wire, unprobed_model), stream=False)
    else:
        _run_embed(_embed_config("ollama", wire, unprobed_model))

    assert wire.ambient.requests == []
