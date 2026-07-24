from __future__ import annotations

from typing import Any

from harborrag_adapters.models.runtime.lifecycle import (
    AsyncLifecycleResource,
    LifecycleResource,
    ResourceOwnership,
)


class ChatClientResourcesMixin:
    """Build ordered lifecycle resources for the concrete chat client."""

    _health_monitor: Any
    _invocation: Any
    _resource_ownership: ResourceOwnership
    _execution: Any
    _telemetry: Any
    _owns_telemetry: bool
    _services: Any
    _owns_services: bool

    def _sync_resources(self) -> tuple[LifecycleResource, ...]:
        resources: list[LifecycleResource] = []
        if self._health_monitor is not None:
            resources.append(LifecycleResource(self._health_monitor.close))
        resources.extend(
            (
                LifecycleResource(self._invocation.close, self._resource_ownership),
                LifecycleResource(
                    self._execution.cache.backend.close,
                    (
                        ResourceOwnership.OWNED
                        if self._execution.owns_cache
                        else ResourceOwnership.BORROWED
                    ),
                ),
                LifecycleResource(
                    self._telemetry.close,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_telemetry
                        else ResourceOwnership.BORROWED
                    ),
                ),
                LifecycleResource(
                    self._services.close,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_services
                        else ResourceOwnership.BORROWED
                    ),
                ),
            )
        )
        return tuple(resources)

    def _async_resources(self) -> tuple[AsyncLifecycleResource, ...]:
        resources: list[AsyncLifecycleResource] = []
        if self._health_monitor is not None:
            resources.append(AsyncLifecycleResource(self._health_monitor.aclose))
        resources.extend(
            (
                AsyncLifecycleResource(self._invocation.aclose, self._resource_ownership),
                AsyncLifecycleResource(
                    self._execution.cache.backend.aclose,
                    (
                        ResourceOwnership.OWNED
                        if self._execution.owns_cache
                        else ResourceOwnership.BORROWED
                    ),
                ),
                AsyncLifecycleResource(
                    self._telemetry.aclose,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_telemetry
                        else ResourceOwnership.BORROWED
                    ),
                ),
                AsyncLifecycleResource(
                    self._services.aclose,
                    (
                        ResourceOwnership.OWNED
                        if self._owns_services
                        else ResourceOwnership.BORROWED
                    ),
                ),
            )
        )
        return tuple(resources)
