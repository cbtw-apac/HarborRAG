"""SDK configuration loading kept separate from runtime orchestration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .config.settings import RuntimeSettings
from .contracts import ExecutionMode


class HarborRAGConfig(BaseModel):
    """Typed SDK construction contract; environment loading remains in runtime."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    execution_mode: ExecutionMode = ExecutionMode.DIRECT
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    discover_plugins: bool = False

    @classmethod
    def from_file(cls, path: str | Path) -> HarborRAGConfig:
        source = Path(path)
        values = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        if not isinstance(values, dict):
            raise ValueError("HarborRAG configuration root must be a mapping")
        unknown = set(values) - {"execution_mode", "discover_plugins", "runtime"}
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ValueError(f"Unknown HarborRAG configuration fields: {names}")
        runtime = values.get("runtime", {})
        if not isinstance(runtime, dict):
            raise ValueError("HarborRAG runtime configuration must be a mapping")
        return cls(
            execution_mode=values.get("execution_mode", ExecutionMode.DIRECT),
            discover_plugins=values.get("discover_plugins", False),
            runtime=RuntimeSettings(**runtime),
        )
