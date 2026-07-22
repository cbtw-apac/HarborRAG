from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import BaseModel, Field

from harborrag_adapters.models.common.budget import (
    BudgetEstimationError,
    BudgetExceededError,
    InMemoryBudgetPolicy,
    NoopBudgetPolicy,
    estimate_request_tokens,
)
from harborrag_adapters.models.common.distributed_config import (
    BudgetPolicyConfig,
)
from harborrag_adapters.models.common.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.common.redis_config import RedisConnectionConfig
from harborrag_adapters.models.common.singleflight import (
    InMemorySingleFlight,
    NoopSingleFlight,
    RedisSingleFlight,
    SingleFlightTimeoutError,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class Metadata(BaseModel):
    tenant_id: str | None = None


class Request(BaseModel):
    metadata: Metadata = Field(default_factory=Metadata)
    token_budget: int | None = None
    prompt: str = "hello"


class Response(BaseModel):
    value: str
    estimated_cost_usd: float | None = None


class RedisSync:
    def __init__(self, leaders: list[bool]) -> None:
        self.leaders = leaders
        self.eval_calls = 0
        self.closed = 0

    def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        del name, value, kwargs
        return self.leaders.pop(0)

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        del script, numkeys, args
        self.eval_calls += 1
        return 1

    def get(self, name: str) -> Any:
        del name
        return None

    def delete(self, *names: str) -> int:
        del names
        return 0

    def hgetall(self, name: str) -> dict[str, str]:
        del name
        return {}

    def close(self) -> None:
        self.closed += 1


class RedisAsync:
    def __init__(self, sync: RedisSync) -> None:
        self.sync = sync
        self.closed = 0

    async def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        return self.sync.set(name, value, **kwargs)

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        return self.sync.eval(script, numkeys, *args)

    async def get(self, name: str) -> Any:
        return self.sync.get(name)

    async def delete(self, *names: str) -> int:
        return self.sync.delete(*names)

    async def hgetall(self, name: str) -> dict[str, str]:
        return self.sync.hgetall(name)

    async def aclose(self) -> None:
        self.closed += 1


def redis_lifecycle(
    leaders: list[bool],
) -> tuple[RedisConnectionLifecycle, RedisSync, RedisAsync]:
    sync = RedisSync(leaders)
    async_client = RedisAsync(sync)
    return (
        RedisConnectionLifecycle(
            RedisConnectionConfig(url="redis://localhost"),
            sync_client=sync,
            async_client=async_client,
            owns_clients=True,
        ),
        sync,
        async_client,
    )


def test_noop_budget_and_token_estimation() -> None:
    request = Request(token_budget=25)
    policy = NoopBudgetPolicy()
    authorization = policy.authorize(request, logical_model="primary")
    assert authorization.scope == "primary"
    assert authorization.estimated_tokens == 0
    policy.settle(authorization, Response(value="ok"))
    assert estimate_request_tokens(request) == 25
    assert estimate_request_tokens(Request(prompt="a" * 40)) > 1


def test_estimate_request_tokens_does_not_undercount_non_ascii_text() -> None:
    ascii_tokens = estimate_request_tokens(Request(prompt="a" * 40))
    # Same character count, but CJK text should never estimate to fewer
    # tokens than the equivalent-length ASCII text -- a flat chars/4 ratio
    # would undercount this and let it slip under a token-rate budget.
    cjk_tokens = estimate_request_tokens(Request(prompt="漢" * 40))
    assert cjk_tokens > ascii_tokens
    assert cjk_tokens >= 40


@pytest.mark.asyncio
async def test_noop_budget_async() -> None:
    policy = NoopBudgetPolicy()
    authorization = await policy.aauthorize(Request(), logical_model="m")
    await policy.asettle(authorization, Response(value="ok"))
    assert authorization.scope == "m"


def test_budget_request_limits_rates_and_scope() -> None:
    request = Request(metadata=Metadata(tenant_id="tenant"), token_budget=10)
    config = BudgetPolicyConfig(
        enabled=True,
        max_request_tokens=10,
        max_request_cost_usd=0.5,
        rpm_limit=1,
        tpm_limit=10,
        daily_cost_usd=1,
        monthly_cost_usd=2,
    )
    policy = InMemoryBudgetPolicy(config, cost_estimator=lambda _request, _model: 0.25)
    authorization = policy.authorize(request, logical_model="primary")
    assert authorization.scope == "tenant:primary"
    assert policy.snapshot("tenant:primary")["requests"] == 1
    with pytest.raises(BudgetExceededError, match="request rate"):
        policy.authorize(request, logical_model="primary")

    with pytest.raises(BudgetExceededError, match="tenant_id"):
        InMemoryBudgetPolicy(config, cost_estimator=lambda _r, _m: 0.1).authorize(
            Request(token_budget=1), logical_model="primary"
        )
    with pytest.raises(BudgetExceededError, match="token budget"):
        InMemoryBudgetPolicy(config, cost_estimator=lambda _r, _m: 0.1).authorize(
            Request(
                metadata=Metadata(tenant_id="tenant"),
                token_budget=11,
            ),
            logical_model="primary",
        )
    with pytest.raises(BudgetExceededError, match="cost budget"):
        InMemoryBudgetPolicy(config, cost_estimator=lambda _r, _m: 0.6).authorize(
            request, logical_model="primary"
        )
    with pytest.raises(BudgetEstimationError):
        InMemoryBudgetPolicy(config).authorize(request, logical_model="primary")


def test_budget_settlement_and_window_reset() -> None:
    now = [1_700_000_000.0]
    config = BudgetPolicyConfig(
        enabled=True,
        require_tenant_id=False,
        rpm_limit=1,
        daily_cost_usd=0.5,
        monthly_cost_usd=0.75,
    )
    policy = InMemoryBudgetPolicy(config, clock=lambda: now[0])
    auth = policy.authorize(Request(token_budget=1), logical_model="m")
    assert auth.scope == "__unscoped__:m"
    policy.settle(auth, Response(value="ok", estimated_cost_usd=None))
    policy.settle(auth, Response(value="ok", estimated_cost_usd=0.4))
    policy.settle(auth, Response(value="ok", estimated_cost_usd=0.2))
    assert policy.snapshot("__unscoped__:m")["day_cost_usd"] == pytest.approx(0.6)
    now[0] += 60
    with pytest.raises(BudgetExceededError, match="daily"):
        policy.authorize(Request(token_budget=1), logical_model="m")
    now[0] += 86_400
    auth = policy.authorize(Request(token_budget=1), logical_model="m")
    policy.settle(auth, Response(value="ok", estimated_cost_usd=0.3))
    now[0] += 60
    with pytest.raises(BudgetExceededError, match="monthly"):
        policy.authorize(Request(token_budget=1), logical_model="m")


@pytest.mark.asyncio
async def test_budget_async_authorize_and_settle() -> None:
    policy = InMemoryBudgetPolicy(
        BudgetPolicyConfig(enabled=True, require_tenant_id=False),
        cost_estimator=lambda _r, _m: 0.1,
    )
    authorization = await policy.aauthorize(Request(), logical_model="m")
    await policy.asettle(authorization, Response(value="ok", estimated_cost_usd=0.1))
    assert policy.snapshot("__unscoped__:m")["day_cost_usd"] == 0.1


def test_noop_and_memory_singleflight_sync() -> None:
    noop = NoopSingleFlight()
    assert noop.execute("k", lambda: Response(value="x"), lambda: None).shared is False
    noop.close()

    coordinator = InMemorySingleFlight()
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def producer() -> Response:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(2)
        return Response(value="shared")

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(coordinator.execute, "k", producer, lambda: None)
        started.wait(2)
        follower = pool.submit(coordinator.execute, "k", producer, lambda: None)
        time.sleep(0.02)
        release.set()
        results = [leader.result(), follower.result()]
    assert calls == 1
    assert sorted(item.shared for item in results) == [False, True]
    coordinator.close()


def test_memory_singleflight_propagates_error() -> None:
    coordinator = InMemorySingleFlight()
    with pytest.raises(RuntimeError, match="boom"):
        coordinator.execute("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")), lambda: None)
    coordinator.close()


@pytest.mark.asyncio
async def test_memory_and_noop_singleflight_async() -> None:
    noop = NoopSingleFlight()

    async def value() -> Response:
        return Response(value="x")

    assert (await noop.aexecute("k", value, lambda: value())).shared is False
    await noop.aclose()

    coordinator = InMemorySingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def producer() -> Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return Response(value="shared")

    leader = asyncio.create_task(coordinator.aexecute("k", producer, value))
    await started.wait()
    follower = asyncio.create_task(coordinator.aexecute("k", producer, value))
    release.set()
    results = await asyncio.gather(leader, follower)
    assert calls == 1
    assert sorted(item.shared for item in results) == [False, True]
    await coordinator.aclose()


def test_redis_singleflight_leader_follower_timeout_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections, sync, async_client = redis_lifecycle([True, False, False])
    coordinator = RedisSingleFlight(
        connections,
        key_prefix="harbor",
        lock_ttl_seconds=2,
        follower_timeout_seconds=0.02,
        poll_interval_seconds=0.001,
        owns_connections=True,
    )
    leader = coordinator.execute("a", lambda: Response(value="leader"), lambda: None)
    assert not leader.shared and sync.eval_calls == 1
    follower = coordinator.execute(
        "b", lambda: Response(value="bad"), lambda: Response(value="cache")
    )
    assert follower.shared and follower.value.value == "cache"
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(SingleFlightTimeoutError):
        coordinator.execute("c", lambda: Response(value="bad"), lambda: None)
    coordinator.close()
    assert sync.closed == async_client.closed == 1


@pytest.mark.asyncio
async def test_redis_singleflight_async_paths() -> None:
    connections, sync, async_client = redis_lifecycle([True, False])
    coordinator = RedisSingleFlight(
        connections,
        key_prefix="harbor",
        lock_ttl_seconds=2,
        follower_timeout_seconds=0.05,
        poll_interval_seconds=0.001,
        owns_connections=True,
    )

    async def leader_value() -> Response:
        return Response(value="leader")

    async def cached() -> Response | None:
        return Response(value="cache")

    assert not (await coordinator.aexecute("a", leader_value, cached)).shared
    assert (await coordinator.aexecute("b", leader_value, cached)).shared
    await coordinator.aclose()
    assert sync.closed == async_client.closed == 1
