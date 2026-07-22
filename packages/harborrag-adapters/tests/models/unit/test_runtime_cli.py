from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from model_runtime_support import (
    FakeChatInvocation,
    FakeEmbeddingInvocation,
    FakeRerankInvocation,
    chat_config,
    embed_config,
    rerank_config,
)

from harborrag_adapters.models.chat import HarborChatClient
from harborrag_adapters.models.cli import ModelConfigCli, main
from harborrag_adapters.models.common.budget import (
    InMemoryBudgetPolicy,
    NoopBudgetPolicy,
)
from harborrag_adapters.models.common.cache import InMemoryModelCache
from harborrag_adapters.models.common.config import (
    CacheBackend,
    CacheConfig,
    RoutingConfig,
    RoutingStrategy,
)
from harborrag_adapters.models.common.distributed_config import (
    BudgetPolicyConfig,
    RoutingStateBackend,
    SingleFlightBackend,
    SingleFlightConfig,
)
from harborrag_adapters.models.common.introspection import ModelRuntimeIntrospector
from harborrag_adapters.models.common.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.common.redis_config import RedisConnectionConfig
from harborrag_adapters.models.common.routing_state import RoutingStateSnapshot
from harborrag_adapters.models.common.routing_state_memory import (
    InMemoryRoutingStateStore,
)
from harborrag_adapters.models.common.routing_state_redis import RedisRoutingStateStore
from harborrag_adapters.models.common.runtime_services import (
    ModelRuntimeServices,
    build_runtime_services,
)
from harborrag_adapters.models.common.singleflight import (
    InMemorySingleFlight,
    RedisSingleFlight,
)
from harborrag_adapters.models.embed import HarborEmbedClient
from harborrag_adapters.models.rerank import HarborRerankingClient

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class SyncRedis:
    def get(self, name: str) -> Any:
        del name
        return None

    def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        del name, value, kwargs
        return True

    def delete(self, *names: str) -> int:
        del names
        return 0

    def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        del script, numkeys, args
        return [1, "ok"]

    def hgetall(self, name: str) -> dict[str, str]:
        del name
        return {}

    def close(self) -> None:
        return None


class AsyncRedis:
    async def get(self, name: str) -> Any:
        del name
        return None

    async def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        del name, value, kwargs
        return True

    async def delete(self, *names: str) -> int:
        del names
        return 0

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        del script, numkeys, args
        return [1, "ok"]

    async def hgetall(self, name: str) -> dict[str, str]:
        del name
        return {}

    async def aclose(self) -> None:
        return None


def connections() -> RedisConnectionLifecycle:
    return RedisConnectionLifecycle(
        RedisConnectionConfig(url="redis://localhost"),
        sync_client=SyncRedis(),
        async_client=AsyncRedis(),
        owns_clients=False,
    )


class SelectorFake:
    def __init__(self, snapshots: dict[str, RoutingStateSnapshot]) -> None:
        self._snapshots = snapshots

    def snapshots(self) -> dict[str, RoutingStateSnapshot]:
        return self._snapshots

    async def asnapshots(self) -> dict[str, RoutingStateSnapshot]:
        return self._snapshots


def test_runtime_introspection_all_strategies() -> None:
    config = chat_config()
    deployment = config.models["primary"].deployments[0]
    model = config.models["primary"].model_copy(update={"aliases": ("main",)})
    config = config.model_copy(update={"models": {"primary": model}})
    selector = SelectorFake({"primary:openai-a": RoutingStateSnapshot(last_latency_ms=5)})
    introspector = ModelRuntimeIntrospector(
        config,
        config.models,
        selector,
        family="chat",
        backend="direct_sdk",  # type: ignore[arg-type]
    )
    description = introspector.describe()
    assert description.aliases == {"main": "primary"}
    assert introspector.list_models() == ("primary",)
    assert introspector.health()[0].available
    assert introspector.explain_route("primary").preview_selection == deployment.name
    assert pytest.run(asyncio=False) if False else True  # retain a simple no-op branch
    with pytest.raises(KeyError, match="unknown logical"):
        introspector.explain_route("missing")


@pytest.mark.asyncio
async def test_runtime_introspection_async_and_exclusions() -> None:
    config = chat_config()
    selector = SelectorFake(
        {
            "primary:openai-a": RoutingStateSnapshot(
                active_healthy=False,
                active_checked_at=10_000_000_000,
            )
        }
    )
    introspector = ModelRuntimeIntrospector(
        config,
        config.models,
        selector,
        family="chat",
        backend="direct",  # type: ignore[arg-type]
    )
    assert not (await introspector.ahealth())[0].available
    explanation = introspector.explain_route("primary")
    assert explanation.preview_selection is None
    assert explanation.excluded_deployments == {"openai-a": "active_health_failed"}


def test_runtime_service_builder_memory_and_redis() -> None:
    memory_config = chat_config().model_copy(
        update={
            "cache": CacheConfig(enabled=True, backend=CacheBackend.CUSTOM),
            "singleflight": SingleFlightConfig(
                backend=SingleFlightBackend.MEMORY,
                lock_ttl_seconds=2,
                follower_timeout_seconds=3,
            ),
            "budget": BudgetPolicyConfig(enabled=True, require_tenant_id=False),
        }
    )
    cache = InMemoryModelCache(max_entries=5)
    services = build_runtime_services(memory_config, family="chat", cache=cache)
    assert services.cache is cache
    assert isinstance(services.routing_state, InMemoryRoutingStateStore)
    assert isinstance(services.singleflight, InMemorySingleFlight)
    assert isinstance(services.budget, InMemoryBudgetPolicy)
    services.close()

    redis_config = chat_config().model_copy(
        update={
            "redis": RedisConnectionConfig(url="redis://localhost", key_prefix="test"),
            "cache": CacheConfig(enabled=True, backend=CacheBackend.REDIS),
            "routing": RoutingConfig(
                strategy=RoutingStrategy.ORDERED,
                state_backend=RoutingStateBackend.REDIS,
            ),
            "singleflight": SingleFlightConfig(
                backend=SingleFlightBackend.REDIS,
                lock_ttl_seconds=2,
                follower_timeout_seconds=3,
            ),
        }
    )
    lifecycle = connections()
    redis_services = build_runtime_services(redis_config, family="chat", redis=lifecycle)
    assert isinstance(redis_services.routing_state, RedisRoutingStateStore)
    assert isinstance(redis_services.singleflight, RedisSingleFlight)
    assert isinstance(redis_services.budget, NoopBudgetPolicy)
    assert redis_services.redis is None


@pytest.mark.asyncio
async def test_runtime_services_close_custom_components() -> None:
    calls: list[str] = []

    class Closable:
        def close(self) -> None:
            calls.append("close")

        async def aclose(self) -> None:
            calls.append("aclose")

    services = ModelRuntimeServices(
        cache=None,
        routing_state=Closable(),  # type: ignore[arg-type]
        singleflight=Closable(),  # type: ignore[arg-type]
        budget=NoopBudgetPolicy(),
        redis=None,
    )
    services.close()
    await services.aclose()
    assert calls == ["close", "close", "aclose", "aclose"]


def write_chat_config(path: Path) -> None:
    config = chat_config()
    path.write_text(json.dumps({"chat": config.model_dump(mode="json")}), encoding="utf-8")


def test_config_cli_validate_render_explain_and_errors(tmp_path: Path, capsys: Any) -> None:
    source = tmp_path / "models.json"
    write_chat_config(source)
    assert main(["validate", str(source), "--family", "chat"]) == 0
    assert "valid chat" in capsys.readouterr().out

    output = tmp_path / "rendered.yaml"
    assert (
        ModelConfigCli().run(["render", str(source), "--family", "chat", "--output", str(output)])
        == 0
    )
    rendered = output.read_text(encoding="utf-8")
    assert "api_key: '**********'" in rendered
    assert "api_key: secret" not in rendered

    assert main(["explain", str(source), "--family", "chat"]) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["default_model"] == "primary"

    bad = tmp_path / "bad.yaml"
    bad.write_text("chat: {}", encoding="utf-8")
    assert main(["validate", str(bad), "--family", "chat"]) == 2
    assert "configuration error" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_public_clients_expose_one_runtime_introspection_api() -> None:
    clients = (
        HarborChatClient(chat_config(), invocation=FakeChatInvocation([])),
        HarborEmbedClient(embed_config(), invocation=FakeEmbeddingInvocation([])),
        HarborRerankingClient(rerank_config(), invocation=FakeRerankInvocation([])),
    )
    try:
        for client in clients:
            assert client.describe().default_model == "primary"
            assert client.list_models() == ("primary",)
            assert client.deployment_health()[0].available
            assert (await client.adeployment_health())[0].available
            assert client.explain_route("primary").preview_selection is not None
            with pytest.raises(RuntimeError, match="active health"):
                client.check_deployment_health()
            with pytest.raises(RuntimeError, match="active health"):
                await client.acheck_deployment_health()
    finally:
        for client in clients:
            await client.aclose()
