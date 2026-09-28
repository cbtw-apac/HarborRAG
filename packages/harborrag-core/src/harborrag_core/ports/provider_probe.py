"""ProviderProbePort: the sole sanctioned boundary for testing a live provider.

Test-connection needs the provider's *raw* secret value to make a real call,
but the API/app layers must never touch it directly (same rule ``SecretsPort``
already states for every other caller). The concrete adapter behind this port
is the one place allowed to call ``SecretsPort.resolve()`` for a provider's
``secret_ref`` -- the app layer passes the ``Provider`` aggregate through
untouched and only ever sees this port's result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harborrag_core.domain.provider import Provider


@dataclass(slots=True, frozen=True)
class ProviderProbeResult:
    """Outcome of one test-connection attempt against a chat provider."""

    ok: bool
    message: str
    latency_ms: float


class ProviderProbePort(Protocol):
    """Make one small real call to verify a provider connection works.

    Callers must only invoke this for chat-family providers -- embedding and
    reranker providers are out of scope for v1 and should never reach this
    port (the caller returns ``HarborCapabilityError`` before it would).
    """

    async def probe(self, provider: Provider) -> ProviderProbeResult:
        """Test ``provider``'s connection with one minimal real request."""
