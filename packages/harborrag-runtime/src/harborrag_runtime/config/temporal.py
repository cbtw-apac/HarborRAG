"""Validated Temporal connection, workflow, and worker configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from harborrag_runtime.errors import RuntimeConfigurationError

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


def _is_loopback(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


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
    allow_insecure_remote: bool = False

    def __post_init__(self) -> None:
        if not self.target.strip() or not self.namespace.strip():
            raise RuntimeConfigurationError("Temporal target and namespace must be non-empty")
        host = urlsplit(f"//{self.target}").hostname
        if host is None:
            raise RuntimeConfigurationError("Temporal target must include a valid host")
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
    """Connection and capacity settings shared by the client and worker."""

    connection: TemporalConnectionConfig = TemporalConnectionConfig()
    worker: WorkerConfig = WorkerConfig()
    workflow_execution_timeout_seconds: int = 2_592_000
    workflow_task_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        values = (
            self.workflow_execution_timeout_seconds,
            self.workflow_task_timeout_seconds,
        )
        if any(value < 1 for value in values):
            raise RuntimeConfigurationError("Temporal workflow timeouts must be positive")

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> TemporalRuntimeConfig:
        """Reuse the existing environment settings object without a new loader."""

        return cls(
            connection=TemporalConnectionConfig(
                target=settings.temporal_target,
                namespace=settings.temporal_namespace,
                identity=settings.temporal_identity,
                api_key=(
                    settings.temporal_api_key.get_secret_value()
                    if settings.temporal_api_key is not None
                    else None
                ),
                tls=TemporalTLSConfig(enabled=settings.temporal_tls),
                allow_insecure_remote=settings.temporal_allow_insecure_remote,
            ),
            worker=WorkerConfig(
                identity=settings.temporal_identity,
                max_concurrent_activities=settings.temporal_max_concurrent_activities,
                max_concurrent_workflow_tasks=settings.temporal_max_concurrent_workflow_tasks,
                max_concurrent_activity_polls=settings.temporal_max_concurrent_activity_polls,
                max_concurrent_workflow_polls=settings.temporal_max_concurrent_workflow_polls,
                graceful_shutdown_seconds=settings.temporal_graceful_shutdown_seconds,
            ),
        )
