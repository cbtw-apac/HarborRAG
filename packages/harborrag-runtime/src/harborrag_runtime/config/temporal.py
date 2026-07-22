"""Validated Temporal connection, workflow, and worker configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_runtime.errors import RuntimeConfigurationError
from harborrag_runtime.temporal.models import WorkflowOptions
from harborrag_runtime.temporal.retry import ActivityRetryConfig
from harborrag_runtime.temporal.task_queues import TaskQueueConfig

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


@dataclass(frozen=True, slots=True)
class TemporalTLSConfig:
    """TLS material supplied by the process secret/configuration layer."""

    enabled: bool = False
    domain: str | None = None
    server_root_ca_cert: bytes | None = field(default=None, repr=False)
    client_cert: bytes | None = field(default=None, repr=False)
    client_private_key: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.client_cert is None) != (self.client_private_key is None):
            raise RuntimeConfigurationError(
                "Temporal client certificate and private key must be configured together"
            )
        if not self.enabled and any(
            value is not None
            for value in (
                self.domain,
                self.server_root_ca_cert,
                self.client_cert,
                self.client_private_key,
            )
        ):
            raise RuntimeConfigurationError("Temporal TLS material requires TLS to be enabled")


@dataclass(frozen=True, slots=True)
class TemporalConnectionConfig:
    target: str = "localhost:7233"
    namespace: str = "harborrag"
    identity: str | None = None
    api_key: str | None = field(default=None, repr=False)
    tls: TemporalTLSConfig = TemporalTLSConfig()

    def __post_init__(self) -> None:
        if not self.target.strip() or not self.namespace.strip():
            raise RuntimeConfigurationError("Temporal target and namespace must be non-empty")


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    identity: str = "harborrag-runtime"
    max_concurrent_activities: int = 32
    max_concurrent_workflow_tasks: int = 16
    max_concurrent_activity_polls: int = 8
    max_concurrent_workflow_polls: int = 5
    graceful_shutdown_seconds: int = 30

    def __post_init__(self) -> None:
        values = (
            self.max_concurrent_activities,
            self.max_concurrent_workflow_tasks,
            self.max_concurrent_activity_polls,
            self.max_concurrent_workflow_polls,
            self.graceful_shutdown_seconds,
        )
        if not self.identity.strip() or any(value < 1 for value in values):
            raise RuntimeConfigurationError("Worker identity and capacity values must be positive")


@dataclass(frozen=True, slots=True)
class TemporalRuntimeConfig:
    """One cohesive configuration consumed by clients, workflows, and workers."""

    connection: TemporalConnectionConfig = TemporalConnectionConfig()
    task_queues: TaskQueueConfig = TaskQueueConfig()
    retries: ActivityRetryConfig = ActivityRetryConfig()
    worker: WorkerConfig = WorkerConfig()
    partition_size: int = 50
    partition_concurrency: int = 4
    artifact_concurrency: int = 16
    continue_after_partitions: int = 100
    continue_after_artifacts: int = 2_000
    workflow_execution_timeout_seconds: int = 2_592_000
    workflow_task_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        values = (
            self.partition_size,
            self.partition_concurrency,
            self.artifact_concurrency,
            self.continue_after_partitions,
            self.continue_after_artifacts,
            self.workflow_execution_timeout_seconds,
            self.workflow_task_timeout_seconds,
        )
        if any(value < 1 for value in values):
            raise RuntimeConfigurationError("Runtime workflow limits and timeouts must be positive")

    def workflow_options(self) -> WorkflowOptions:
        """Freeze workflow-affecting configuration into workflow input."""

        return WorkflowOptions(
            task_queues=self.task_queues,
            retries=self.retries,
            partition_size=self.partition_size,
            partition_concurrency=self.partition_concurrency,
            artifact_concurrency=self.artifact_concurrency,
            continue_after_partitions=self.continue_after_partitions,
            continue_after_artifacts=self.continue_after_artifacts,
        )

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> TemporalRuntimeConfig:
        """Reuse the existing environment settings object without a new loader."""

        return cls(
            connection=TemporalConnectionConfig(
                target=settings.temporal_target,
                namespace=settings.temporal_namespace,
                identity=settings.temporal_identity,
                api_key=settings.temporal_api_key,
                tls=TemporalTLSConfig(enabled=settings.temporal_tls),
            ),
            worker=WorkerConfig(identity=settings.temporal_identity),
            partition_size=settings.ingestion_partition_size,
            partition_concurrency=settings.partition_concurrency,
            artifact_concurrency=settings.artifact_concurrency,
        )
