from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harborrag_adapters.models.chat.backend import ChatBackend
from harborrag_adapters.models.chat.backend_config import ChatBackendType


@dataclass
class CallRecorder:
    sync_calls: list[dict[str, Any]] = field(default_factory=list)
    async_calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, **kwargs: Any) -> dict[str, Any]:
        self.sync_calls.append(kwargs)
        return kwargs

    async def acomplete(self, **kwargs: Any) -> dict[str, Any]:
        self.async_calls.append(kwargs)
        return kwargs


@dataclass
class BackendContractHarness:
    backend: ChatBackend
    recorder: CallRecorder
    expected_type: ChatBackendType
    expected_model: str
    assert_parameters: Callable[[dict[str, Any]], None]


async def exercise_backend_contract(harness: BackendContractHarness) -> None:
    assert harness.backend.backend_type is harness.expected_type
    sync_result = harness.backend.complete(model="alias", stream=False)
    async_result = await harness.backend.acomplete(model="alias", stream=False)
    sync_stream = harness.backend.stream(model="alias", stream=True)
    async_stream = await harness.backend.astream(model="alias", stream=True)

    for result in (sync_result, async_result, sync_stream, async_stream):
        assert result["model"] == harness.expected_model
        harness.assert_parameters(result)
    harness.backend.close_stream(object())
    await harness.backend.aclose_stream(object())


class FakeRouter:
    def __init__(self, recorder: CallRecorder) -> None:
        self.recorder = recorder
        self.flushed = 0
        self.closed = 0

    def completion(self, **kwargs: Any) -> dict[str, Any]:
        return self.recorder.complete(**kwargs)

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        return await self.recorder.acomplete(**kwargs)

    def flush_cache(self) -> None:
        self.flushed += 1

    async def aclose(self) -> None:
        self.closed += 1


class FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1
