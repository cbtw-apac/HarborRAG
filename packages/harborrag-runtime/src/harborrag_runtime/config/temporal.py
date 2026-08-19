"""Validated Temporal connection, workflow, and worker configuration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from ipaddress import ip_address
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from harborrag_runtime.errors import RuntimeConfigurationError
from harborrag_runtime.ingestion.limits import MAX_BATCH_SIZE, MAX_DOCUMENT_CONCURRENCY
from harborrag_runtime.temporal_models import (
    ActivityRetryConfig,
    TaskQueueConfig,
    TemporalWorkflowOptions,
)

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


TEMPORAL_CONFIG_VERSION = 1


def _is_loopback(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _target_host(target: str) -> str:
    """Validate an SDK gRPC authority and return its host component."""

    try:
        parsed = urlsplit(f"//{target}")
        port = parsed.port
    except ValueError as error:
        raise RuntimeConfigurationError(
            "Temporal target must be a valid host:port gRPC authority"
        ) from error
    if (
        parsed.hostname is None
        or port is None
        or port < 1
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc != target
    ):
        raise RuntimeConfigurationError(
            "Temporal target must be a host:port gRPC authority without a URL "
            "scheme, credentials, path, query, or fragment (7233 for the local "
            "frontend; 8080 is the Web UI)"
        )
    return parsed.hostname


@dataclass(frozen=True, slots=True)
class TemporalTLSConfig:
    """TLS options; certificate bytes may be supplied programmatically."""

    enabled: bool = False
    domain: str | None = None
    server_root_ca_cert: bytes | None = field(default=None, repr=False)
    client_cert: bytes | None = field(default=None, repr=False)
    client_private_key: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.domain is not None and self.domain != self.domain.strip():
            raise RuntimeConfigurationError("Temporal TLS domain must not contain outer whitespace")
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
    allow_insecure_remote: bool = False

    def __post_init__(self) -> None:
        values = (self.target, self.namespace)
        if any(not value.strip() or value != value.strip() for value in values):
            raise RuntimeConfigurationError("Temporal target and namespace must be non-empty")
        if self.identity is not None and self.identity != self.identity.strip():
            raise RuntimeConfigurationError("Temporal identity must not contain outer whitespace")
        host = _target_host(self.target)
        loopback = _is_loopback(host)
        if self.api_key and not self.tls.enabled:
            raise RuntimeConfigurationError("Temporal api_key requires TLS")
        if not self.tls.enabled and not loopback and not self.allow_insecure_remote:
            raise RuntimeConfigurationError(
                "remote Temporal requires TLS; set allow_insecure_remote only "
                "for an explicitly trusted development network"
            )


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    identity: str = "harborrag-runtime"
    max_concurrent_activities: int = 2
    max_concurrent_workflow_tasks: int = 4
    max_concurrent_activity_polls: int = 2
    max_concurrent_workflow_polls: int = 2
    graceful_shutdown_seconds: int = 30

    def __post_init__(self) -> None:
        bounded_values = (
            (self.max_concurrent_activities, 1_000),
            (self.max_concurrent_workflow_tasks, 1_000),
            (self.max_concurrent_activity_polls, 100),
            (self.max_concurrent_workflow_polls, 100),
            (self.graceful_shutdown_seconds, 3_600),
        )
        if (
            not self.identity.strip()
            or self.identity != self.identity.strip()
            or any(not 1 <= value <= maximum for value, maximum in bounded_values)
        ):
            raise RuntimeConfigurationError("Temporal worker identity or capacity is out of range")


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    """Default per-run source batching; a CLI/API caller may override it."""

    batch_size: int = 200
    document_concurrency: int = 8

    def __post_init__(self) -> None:
        if not 1 <= self.batch_size <= MAX_BATCH_SIZE or not (
            1 <= self.document_concurrency <= MAX_DOCUMENT_CONCURRENCY
        ):
            raise RuntimeConfigurationError(
                "Temporal ingestion batch_size or document_concurrency is out of range"
            )


@dataclass(frozen=True, slots=True)
class TemporalRuntimeConfig:
    """Connection and capacity settings shared by the client and worker."""

    connection: TemporalConnectionConfig = TemporalConnectionConfig()
    worker: WorkerConfig = WorkerConfig()
    task_queues: TaskQueueConfig = TaskQueueConfig()
    retries: ActivityRetryConfig = ActivityRetryConfig()
    ingestion: IngestionConfig = IngestionConfig()
    workflow_execution_timeout_seconds: int = 2_592_000
    workflow_task_timeout_seconds: int = 10
    health_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        workflow_timeouts = (
            self.workflow_execution_timeout_seconds,
            self.workflow_task_timeout_seconds,
        )
        if (
            any(value < 1 for value in workflow_timeouts)
            or not isfinite(self.health_timeout_seconds)
            or self.health_timeout_seconds <= 0
        ):
            raise RuntimeConfigurationError("Temporal timeouts must be positive")

    @classmethod
    def from_file(cls, path: str | Path) -> TemporalRuntimeConfig:
        """Load a versioned Temporal YAML configuration file."""

        from harborrag_runtime.config.temporal_loading import load_temporal_config

        return load_temporal_config(path)

    def workflow_options(self) -> TemporalWorkflowOptions:
        """Freeze configurable routing and retries into workflow history."""

        return TemporalWorkflowOptions(task_queues=self.task_queues, retries=self.retries)

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> TemporalRuntimeConfig:
        """Load YAML and apply explicitly supplied environment overrides.

        The compatibility fallback keeps lightweight SDK composition working
        outside the repository when the default relative file is unavailable.
        An explicitly configured missing path always fails loudly.
        """

        configured_path = Path(settings.temporal_config_path).expanduser()
        explicit_fields = settings.model_fields_set
        if configured_path.is_file() or "temporal_config_path" in explicit_fields:
            config = cls.from_file(configured_path)
        else:
            config = cls()

        connection = config.connection
        worker = config.worker
        api_key = connection.api_key
        if settings.temporal_api_key is not None:
            api_key = settings.temporal_api_key.get_secret_value().strip() or None

        selected_connection = replace(
            connection,
            target=settings.temporal_target
            if "temporal_target" in explicit_fields
            else connection.target,
            namespace=settings.temporal_namespace
            if "temporal_namespace" in explicit_fields
            else connection.namespace,
            identity=settings.temporal_identity
            if "temporal_identity" in explicit_fields
            else connection.identity,
            api_key=api_key,
            tls=(
                replace(connection.tls, enabled=True)
                if settings.temporal_tls
                else TemporalTLSConfig()
            )
            if "temporal_tls" in explicit_fields
            else connection.tls,
            allow_insecure_remote=settings.temporal_allow_insecure_remote
            if "temporal_allow_insecure_remote" in explicit_fields
            else connection.allow_insecure_remote,
        )
        selected_worker = replace(
            worker,
            identity=settings.temporal_worker_identity
            if "temporal_worker_identity" in explicit_fields
            else worker.identity,
            max_concurrent_activities=settings.temporal_max_concurrent_activities
            if "temporal_max_concurrent_activities" in explicit_fields
            else worker.max_concurrent_activities,
            max_concurrent_workflow_tasks=settings.temporal_max_concurrent_workflow_tasks
            if "temporal_max_concurrent_workflow_tasks" in explicit_fields
            else worker.max_concurrent_workflow_tasks,
            max_concurrent_activity_polls=settings.temporal_max_concurrent_activity_polls
            if "temporal_max_concurrent_activity_polls" in explicit_fields
            else worker.max_concurrent_activity_polls,
            max_concurrent_workflow_polls=settings.temporal_max_concurrent_workflow_polls
            if "temporal_max_concurrent_workflow_polls" in explicit_fields
            else worker.max_concurrent_workflow_polls,
            graceful_shutdown_seconds=settings.temporal_graceful_shutdown_seconds
            if "temporal_graceful_shutdown_seconds" in explicit_fields
            else worker.graceful_shutdown_seconds,
        )
        selected_ingestion = replace(
            config.ingestion,
            batch_size=settings.temporal_ingestion_batch_size
            if "temporal_ingestion_batch_size" in explicit_fields
            else config.ingestion.batch_size,
            document_concurrency=settings.temporal_ingestion_document_concurrency
            if "temporal_ingestion_document_concurrency" in explicit_fields
            else config.ingestion.document_concurrency,
        )
        selected = replace(
            config,
            connection=selected_connection,
            worker=selected_worker,
            ingestion=selected_ingestion,
            health_timeout_seconds=settings.temporal_health_timeout_seconds
            if "temporal_health_timeout_seconds" in explicit_fields
            else config.health_timeout_seconds,
        )
        database_capacity = settings.control_db_pool_size + settings.control_db_max_overflow
        queue_count = len(selected.task_queues.as_tuple())
        requested_capacity = selected.worker.max_concurrent_activities * queue_count
        if requested_capacity > database_capacity:
            raise RuntimeConfigurationError(
                f"Temporal activity capacity across {queue_count} task-queue workers "
                f"({requested_capacity}) exceeds the control database pool "
                f"capacity ({database_capacity})"
            )
        return selected
