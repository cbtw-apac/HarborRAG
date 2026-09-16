"""ProviderProbePort implementation: one minimal real LiteLLM call.

This is the one sanctioned place that calls ``SecretsPort.resolve()`` for a
provider's ``secret_ref`` -- see ``ProviderProbePort``'s docstring. It talks
to LiteLLM directly rather than through the YAML-configured deployment stack
in ``harborrag_adapters.models.chat.execution`` (irrelevant here: a control-
plane ``Provider`` row is a per-tenant CRUD resource, not a logical-model
deployment), so ``Provider.config`` only needs a LiteLLM-qualified ``model``
string (e.g. ``"openai/gpt-4o-mini"``) and an optional ``api_base``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.provider import Provider
from harborrag_core.ports.provider_probe import ProviderProbeResult
from harborrag_core.ports.secrets import SecretsPort

type AsyncCompletionCallable = Callable[..., Awaitable[Any]]

_PROBE_TIMEOUT_SECONDS = 10.0
_PROBE_MESSAGE = "ping"


class LiteLLMProviderProbe:
    """ProviderProbePort backed by one real ``litellm.acompletion`` call."""

    def __init__(
        self,
        *,
        secrets: SecretsPort,
        acompletion: AsyncCompletionCallable | None = None,
    ) -> None:
        """Bind an injected completion function or the current LiteLLM entrypoint."""
        if acompletion is None:
            import litellm

            acompletion = litellm.acompletion
        self.secrets = secrets
        self._acompletion = acompletion

    async def probe(self, provider: Provider) -> ProviderProbeResult:
        """Resolve ``provider``'s secret (if any) and attempt one minimal completion."""
        model = provider.config.get("model")
        if not isinstance(model, str) or not model:
            raise HarborValidationError(
                "provider config must set a non-empty 'model' field to test the connection"
            )
        api_key = await self.secrets.resolve(provider.secret_ref) if provider.secret_ref else None
        api_base = provider.config.get("api_base")
        started = time.perf_counter()
        try:
            await self._acompletion(
                model=model,
                messages=[{"role": "user", "content": _PROBE_MESSAGE}],
                api_key=api_key,
                api_base=api_base if isinstance(api_base, str) else None,
                max_tokens=1,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - reported as a probe failure, not raised
            return ProviderProbeResult(
                ok=False,
                message=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        return ProviderProbeResult(
            ok=True,
            message="Provider responded successfully",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
