from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_engine.builder import EngineBuilder

from harborrag_runtime.services.base import BaseRuntimeService
from harborrag_runtime.services.mock import MockRuntimeService


@dataclass(slots=True)
class CompositionRoot:
    engine_builder: EngineBuilder
    runtime_service: BaseRuntimeService = field(default_factory=MockRuntimeService)

    @classmethod
    def local(cls) -> CompositionRoot:
        return cls(engine_builder=EngineBuilder())

    def diagnostics(self) -> dict[str, object]:
        return {
            "runtime": self.runtime_service.diagnostics(),
            "engine": self.engine_builder.diagnostics(),
        }
