"""ProviderProbePort implementation: one minimal real LiteLLM call.

This is the one sanctioned place that calls ``SecretsPort.resolve()`` for a
provider's ``secret_ref`` -- see ``ProviderProbePort``'s docstring. It talks
to LiteLLM directly rather than through the YAML-configured deployment stack
in ``harborrag_adapters.models.chat.execution`` (irrelevant here: a control-
plane ``Provider`` row is a per-tenant CRUD resource, not a logical-model
deployment), so ``Provider.config`` only needs a LiteLLM-qualified ``model``
string (e.g. ``"openai/gpt-4o-mini"``) and an optional ``api_base``. That
``api_base`` is admin-supplied per tenant, so it still goes through
``validate_base_url`` (the same scheme/HTTPS/no-userinfo policy the execution
stack enforces) before any secret is sent to it -- this does not allowlist
hosts, so an admin can still point it at another host on the same private
network as HarborRAG; it only closes the completely-unvalidated gap.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from harborrag_adapters.models.runtime.transport import validate_base_url
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.provider import Provider
from harborrag_core.ports.provider_probe import ProviderProbeResult
from harborrag_core.ports.secrets import SecretsPort

type AsyncCompletionCallable = Callable[..., Awaitable[Any]]

logger = logging.getLogger("harborrag.models.chat.probe")

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
        api_key = (
            await self.secrets.resolve(provider.secret_ref, tenant_id=provider.tenant_id)
            if provider.secret_ref
            else None
        )
        api_base = provider.config.get("api_base")
        if api_base is not None:
            if not isinstance(api_base, str):
                raise HarborValidationError("provider config 'api_base' must be a string")
            try:
                validate_base_url(api_base, allowed_hosts=None, require_https=True)
            except ValueError as exc:
                raise HarborValidationError(
                    f"provider config 'api_base' is invalid: {exc}"
                ) from exc
        started = time.perf_counter()
        try:
            await self._acompletion(
                model=model,
                messages=[{"role": "user", "content": _PROBE_MESSAGE}],
                api_key=api_key,
                api_base=api_base,
                max_tokens=1,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - reported as a probe failure, not raised
            # The raw exception can carry the endpoint URL, response body, or
            # other provider-SDK diagnostics -- log it for operators but keep
            # the public message to the exception type only (path policy: no
            # raw provider responses outside debug/diagnostic fields).
            logger.warning("Provider probe failed for %r: %s", provider.id, exc, exc_info=exc)
            return ProviderProbeResult(
                ok=False,
                message=f"Provider probe failed: {type(exc).__name__}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        return ProviderProbeResult(
            ok=True,
            message="Provider responded successfully",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
