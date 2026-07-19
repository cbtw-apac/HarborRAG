from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from .config import CacheBackend, ModelClientConfig
from .litellm_backend import build_provider_params, litellm_routing_strategy

type ProviderResolver = Callable[[Any], str | None]
type RouterFactory = Callable[..., Any]


def router_model_name(logical_model: str, deployment: str) -> str:
    """Return the private Router group used for an explicitly selected deployment."""

    return f"harbor::{logical_model}::{deployment}"


def build_litellm_router(
    config: ModelClientConfig,
    models: Mapping[str, Any],
    *,
    provider_resolver: ProviderResolver,
    router_factory: RouterFactory | None = None,
) -> Any:
    """Build a LiteLLM Router while Harbor retains retry/fallback policy ownership."""

    from litellm.types.utils import BudgetConfig

    if router_factory is None:
        from litellm import Router

        router_factory = cast(RouterFactory, Router)
    model_list: list[dict[str, Any]] = []
    for logical_name, logical in models.items():
        for deployment in logical.deployments:
            if not deployment.enabled:
                continue
            params = build_provider_params(
                deployment,
                litellm_provider=provider_resolver(deployment),
            )
            limits = {
                "rpm": deployment.rpm,
                "tpm": deployment.tpm,
                "max_parallel_requests": deployment.max_parallel_requests,
            }
            params.update({key: value for key, value in limits.items() if value is not None})
            model_list.append(
                {
                    "model_name": router_model_name(logical_name, deployment.name),
                    "litellm_params": params,
                    "model_info": {
                        "id": f"{logical_name}:{deployment.name}",
                        "weight": deployment.weight,
                        "order": deployment.order,
                    },
                }
            )
    budgets = {
        provider: BudgetConfig(**budget.model_dump(exclude_none=True))
        for provider, budget in config.provider_budgets.items()
    }
    return router_factory(
        model_list=model_list,
        routing_strategy=litellm_routing_strategy(config.routing.strategy),
        num_retries=0,
        max_fallbacks=0,
        disable_cooldowns=True,
        timeout=config.timeouts.request_seconds,
        provider_budget_config=budgets or None,
        cache_responses=(config.cache.enabled and config.cache.backend is CacheBackend.LITELLM),
        cache_kwargs={"ttl": config.cache.ttl_seconds},
    )
