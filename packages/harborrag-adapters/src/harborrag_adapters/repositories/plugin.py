from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from harborrag_adapters.repositories.telemetry import StorageTelemetryHook
from harborrag_core.schemas.storage import StorageFamily


class RepositoryConfig(BaseModel):
    """Common validated configuration shared by provider-specific models."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    instance_name: str = Field(default="default", min_length=1)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    operation_timeout_seconds: float = Field(default=30.0, gt=0)


@dataclass(frozen=True, slots=True)
class RepositoryDependencies:
    """Runtime services injected into provider plugins without engine coupling."""

    telemetry: StorageTelemetryHook | None = None


class RepositoryPlugin[ConfigT: RepositoryConfig, ProductT]:
    """Validates provider options and creates one unconnected backend."""

    name: str
    family: StorageFamily
    config_type: type[ConfigT]
    capabilities: BaseModel | None = None
    optional_dependency: str | None = None

    def create(self, config: ConfigT, dependencies: RepositoryDependencies) -> ProductT:
        """Create one unconnected backend from validated provider configuration."""
        raise NotImplementedError
