from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import BoundedSemaphore
from typing import Protocol
from weakref import WeakKeyDictionary


class DeploymentLike(Protocol):
    """Expose selection and concurrency fields shared by provider deployments."""

    name: str
    enabled: bool
    weight: float
    order: int
    max_parallel_requests: int | None
    rpm: int | None
    tpm: int | None


@dataclass(slots=True)
class DeploymentRuntime[D: DeploymentLike]:
    """Track mutable health and concurrency state for one deployment."""

    config: D
    logical_model: str = ""
    active_requests: int = 0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    last_latency_ms: float | None = None
    distributed_active_requests: int = 0
    sync_semaphore: BoundedSemaphore | None = field(default=None, repr=False)
    _async_semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.BoundedSemaphore] = (
        field(default_factory=WeakKeyDictionary, repr=False)
    )

    def __post_init__(self) -> None:
        if self.config.max_parallel_requests:
            self.sync_semaphore = BoundedSemaphore(self.config.max_parallel_requests)

    def async_semaphore(self) -> asyncio.BoundedSemaphore | None:
        """Return the concurrency semaphore bound to the running event loop."""
        if not self.config.max_parallel_requests:
            return None
        loop = asyncio.get_running_loop()
        semaphore = self._async_semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.BoundedSemaphore(self.config.max_parallel_requests)
            self._async_semaphores[loop] = semaphore
        return semaphore

    def available(self, now: float) -> bool:
        """Return whether the deployment is enabled and outside cooldown."""
        return self.config.enabled and now >= self.circuit_open_until
