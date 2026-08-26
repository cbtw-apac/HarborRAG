"""Model dimension resolution must be deterministic and fail closed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr

from harborrag_runtime.composition.resources import embedding_dimensions
from harborrag_runtime.rate_limiting import build_connector_rate_limiter


def _config(*deployment_dimensions: int | None, default: int | None = None) -> object:
    model = SimpleNamespace(
        deployments=[
            SimpleNamespace(expected_dimensions=dimensions) for dimensions in deployment_dimensions
        ],
        default_params=SimpleNamespace(dimensions=default),
    )
    return SimpleNamespace(model_for=lambda name: (name, model))


@pytest.mark.whitebox
def test_embedding_dimensions_uses_one_unambiguous_deployment_dimension() -> None:
    assert embedding_dimensions(_config(1536, 1536, None), "embedding") == 1536


@pytest.mark.whitebox
def test_embedding_dimensions_falls_back_to_an_explicit_model_default() -> None:
    assert embedding_dimensions(_config(768, 1536, default=3072), "embedding") == 3072


@pytest.mark.whitebox
def test_embedding_dimensions_rejects_an_ambiguous_or_missing_dimension() -> None:
    with pytest.raises(ValueError, match="no unambiguous expected_dimensions"):
        embedding_dimensions(_config(None), "embedding")


@pytest.mark.whitebox
def test_rate_limiter_uses_local_pacing_without_redis() -> None:
    limiter = build_connector_rate_limiter(cast(Any, SimpleNamespace(redis_url=None)))
    assert type(limiter).__name__ == "LocalIntervalRateLimiter"


@pytest.mark.whitebox
def test_rate_limiter_builds_the_configured_redis_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redis import Redis

    client = object()
    calls: list[tuple[str, dict[str, object]]] = []

    def from_url(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return client

    monkeypatch.setattr(Redis, "from_url", from_url)
    settings = SimpleNamespace(
        redis_url=SecretStr("rediss://cache.internal:6379/0"),
        redis_socket_timeout_seconds=2.5,
        connector_rate_limit_key_prefix="harborrag:test",
    )

    limiter = build_connector_rate_limiter(cast(Any, settings))

    assert type(limiter).__name__ == "RedisTokenBucketRateLimiter"
    assert calls == [
        (
            "rediss://cache.internal:6379/0",
            {
                "socket_connect_timeout": 2.5,
                "socket_timeout": 2.5,
                "health_check_interval": 30,
            },
        )
    ]
