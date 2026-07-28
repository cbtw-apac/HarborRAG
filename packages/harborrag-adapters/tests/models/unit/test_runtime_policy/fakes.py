from __future__ import annotations

from typing import Any

from harborrag_adapters.models.chat import HarborChatClientConfig


class Invocation:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.async_calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._next()

    async def acomplete(self, **kwargs: Any) -> Any:
        self.async_calls.append(kwargs)
        return self._next()

    def _next(self) -> Any:
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None: ...

    async def aclose(self) -> None: ...


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def response(text: str = "ok") -> dict[str, Any]:
    return {
        "id": f"response-{text}",
        "model": "provider-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def runtime_config(
    *,
    deployments: int = 1,
    fallback: bool = False,
    attempts: int = 1,
    cache: bool = False,
    ttl: int = 30,
) -> HarborChatClientConfig:
    primary: dict[str, Any] = {
        "fallbacks": ["secondary"] if fallback else [],
        "deployments": [
            {
                "name": f"primary-{index}",
                "provider": "openai",
                "model": f"openai/model-{index}",
                "api_key": "key",
                "order": index,
            }
            for index in range(deployments)
        ],
    }
    models: dict[str, Any] = {"primary": primary}
    if fallback:
        models["secondary"] = {
            "provider": "openai",
            "model": "openai/fallback",
            "api_key": "key",
        }
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "retry": {
                "same_deployment_attempts": attempts,
                "max_deployment_failovers": 5,
                "max_model_fallbacks": 5,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
            },
            "routing": {"strategy": "ordered"},
            "cache": {"enabled": cache, "ttl_seconds": ttl},
            "models": models,
        }
    )
