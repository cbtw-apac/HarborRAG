"""Framework-independent process lifecycle and observation ports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class AsyncLifecyclePort(Protocol):
    """Resource owned by a process and started and closed asynchronously."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...


class RuntimeObserverPort(Protocol):
    """Record a named runtime event without prescribing a telemetry framework."""

    def record(
        self,
        event: str,
        attributes: Mapping[str, str | int | float],
    ) -> None: ...
