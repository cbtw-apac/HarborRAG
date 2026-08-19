"""Strict loader for the versioned Temporal runtime YAML file."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harborrag_runtime.config.errors import TemporalConfigurationError
from harborrag_runtime.config.loading import (
    read_yaml_file,
    reject_unknown_keys,
    require_boolean,
    require_finite_number,
    require_integer,
    require_nonblank_string,
    require_optional_nonblank_string,
    require_schema_version,
    require_string_mapping,
)
from harborrag_runtime.config.temporal import (
    TEMPORAL_CONFIG_VERSION,
    IngestionConfig,
    TemporalConnectionConfig,
    TemporalRuntimeConfig,
    TemporalTLSConfig,
    WorkerConfig,
)
from harborrag_runtime.errors import RuntimeConfigurationError
from harborrag_runtime.temporal_models import (
    ActivityRetryConfig,
    RetryPolicyConfig,
    TaskQueueConfig,
)

_ROOT_KEYS = frozenset(
    {"connection", "health", "ingestion", "retries", "task_queues", "version", "worker", "workflow"}
)
_CONNECTION_KEYS = frozenset({"allow_insecure_remote", "identity", "namespace", "target", "tls"})
_TLS_KEYS = frozenset({"domain", "enabled"})
_WORKER_KEYS = frozenset(
    {
        "graceful_shutdown_seconds",
        "identity",
        "max_concurrent_activities",
        "max_concurrent_activity_polls",
        "max_concurrent_workflow_polls",
        "max_concurrent_workflow_tasks",
    }
)
_WORKFLOW_KEYS = frozenset({"execution_timeout_seconds", "task_timeout_seconds"})
_HEALTH_KEYS = frozenset({"timeout_seconds"})
_INGESTION_KEYS = frozenset({"batch_size", "document_concurrency"})
_TASK_QUEUE_KEYS = frozenset({"discovery", "index", "io", "model", "parser", "transform"})
_RETRY_KEYS = frozenset({"discovery", "document"})
_RETRY_POLICY_KEYS = frozenset(
    {
        "backoff_coefficient",
        "initial_interval_seconds",
        "maximum_attempts",
        "maximum_interval_seconds",
    }
)


def load_temporal_config(path: str | Path) -> TemporalRuntimeConfig:
    """Load and strictly validate Temporal runtime configuration from YAML."""

    _, raw = read_yaml_file(
        path,
        label="Temporal configuration",
        error_type=TemporalConfigurationError,
    )
    root = _mapping(raw, "temporal configuration root")
    _reject_unknown(root, _ROOT_KEYS, "temporal configuration root")
    require_schema_version(
        root.get("version"),
        expected=TEMPORAL_CONFIG_VERSION,
        label="Temporal configuration",
        error_type=TemporalConfigurationError,
    )

    connection = _mapping(root.get("connection", {}), "Temporal connection")
    tls = _mapping(connection.get("tls", {}), "Temporal TLS")
    worker = _mapping(root.get("worker", {}), "Temporal worker")
    workflow = _mapping(root.get("workflow", {}), "Temporal workflow")
    health = _mapping(root.get("health", {}), "Temporal health")
    task_queues = _mapping(root.get("task_queues", {}), "Temporal task queues")
    retries = _mapping(root.get("retries", {}), "Temporal retries")
    ingestion = _mapping(root.get("ingestion", {}), "Temporal ingestion")
    _reject_unknown(connection, _CONNECTION_KEYS, "Temporal connection")
    _reject_unknown(tls, _TLS_KEYS, "Temporal TLS")
    _reject_unknown(worker, _WORKER_KEYS, "Temporal worker")
    _reject_unknown(workflow, _WORKFLOW_KEYS, "Temporal workflow")
    _reject_unknown(health, _HEALTH_KEYS, "Temporal health")
    _reject_unknown(task_queues, _TASK_QUEUE_KEYS, "Temporal task queues")
    _reject_unknown(retries, _RETRY_KEYS, "Temporal retries")
    _reject_unknown(ingestion, _INGESTION_KEYS, "Temporal ingestion")

    defaults = TemporalRuntimeConfig()
    default_connection = defaults.connection
    default_worker = defaults.worker
    default_queues = defaults.task_queues
    default_ingestion = defaults.ingestion
    discovery_retry = _mapping(retries.get("discovery", {}), "Temporal discovery retry")
    document_retry = _mapping(retries.get("document", {}), "Temporal document retry")
    _reject_unknown(discovery_retry, _RETRY_POLICY_KEYS, "Temporal discovery retry")
    _reject_unknown(document_retry, _RETRY_POLICY_KEYS, "Temporal document retry")
    try:
        return TemporalRuntimeConfig(
            connection=TemporalConnectionConfig(
                target=_string(connection, "target", default_connection.target),
                namespace=_string(connection, "namespace", default_connection.namespace),
                identity=_optional_string(
                    connection,
                    "identity",
                    default_connection.identity,
                ),
                tls=TemporalTLSConfig(
                    enabled=_boolean(tls, "enabled", default_connection.tls.enabled),
                    domain=_optional_string(tls, "domain", default_connection.tls.domain),
                ),
                allow_insecure_remote=_boolean(
                    connection,
                    "allow_insecure_remote",
                    default_connection.allow_insecure_remote,
                ),
            ),
            worker=WorkerConfig(
                identity=_string(worker, "identity", default_worker.identity),
                max_concurrent_activities=_integer(
                    worker,
                    "max_concurrent_activities",
                    default_worker.max_concurrent_activities,
                ),
                max_concurrent_workflow_tasks=_integer(
                    worker,
                    "max_concurrent_workflow_tasks",
                    default_worker.max_concurrent_workflow_tasks,
                ),
                max_concurrent_activity_polls=_integer(
                    worker,
                    "max_concurrent_activity_polls",
                    default_worker.max_concurrent_activity_polls,
                ),
                max_concurrent_workflow_polls=_integer(
                    worker,
                    "max_concurrent_workflow_polls",
                    default_worker.max_concurrent_workflow_polls,
                ),
                graceful_shutdown_seconds=_integer(
                    worker,
                    "graceful_shutdown_seconds",
                    default_worker.graceful_shutdown_seconds,
                ),
            ),
            task_queues=TaskQueueConfig(
                discovery=_string(task_queues, "discovery", default_queues.discovery),
                transform=_string(task_queues, "transform", default_queues.transform),
                io=_string(task_queues, "io", default_queues.io),
                parser=_string(task_queues, "parser", default_queues.parser),
                model=_string(task_queues, "model", default_queues.model),
                index=_string(task_queues, "index", default_queues.index),
            ),
            retries=ActivityRetryConfig(
                discovery=_retry_policy(discovery_retry, defaults.retries.discovery),
                document=_retry_policy(document_retry, defaults.retries.document),
            ),
            workflow_execution_timeout_seconds=_integer(
                workflow,
                "execution_timeout_seconds",
                defaults.workflow_execution_timeout_seconds,
            ),
            workflow_task_timeout_seconds=_integer(
                workflow,
                "task_timeout_seconds",
                defaults.workflow_task_timeout_seconds,
            ),
            health_timeout_seconds=_number(
                health,
                "timeout_seconds",
                defaults.health_timeout_seconds,
            ),
            ingestion=IngestionConfig(
                batch_size=_integer(
                    ingestion,
                    "batch_size",
                    default_ingestion.batch_size,
                ),
                document_concurrency=_integer(
                    ingestion,
                    "document_concurrency",
                    default_ingestion.document_concurrency,
                ),
            ),
        )
    except RuntimeConfigurationError as exc:
        raise TemporalConfigurationError(f"Invalid Temporal configuration: {exc}") from exc


def _retry_policy(
    values: Mapping[str, Any],
    default: RetryPolicyConfig,
) -> RetryPolicyConfig:
    return RetryPolicyConfig(
        initial_interval_seconds=_number(
            values,
            "initial_interval_seconds",
            default.initial_interval_seconds,
        ),
        backoff_coefficient=_number(
            values,
            "backoff_coefficient",
            default.backoff_coefficient,
        ),
        maximum_interval_seconds=_number(
            values,
            "maximum_interval_seconds",
            default.maximum_interval_seconds,
        ),
        maximum_attempts=_integer(values, "maximum_attempts", default.maximum_attempts),
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    return require_string_mapping(
        value,
        label=label,
        error_type=TemporalConfigurationError,
    )


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    reject_unknown_keys(
        value,
        allowed,
        label=label,
        error_type=TemporalConfigurationError,
    )


def _string(values: Mapping[str, Any], name: str, default: str) -> str:
    return require_nonblank_string(
        values.get(name, default),
        label=f"Temporal {name}",
        error_type=TemporalConfigurationError,
    )


def _optional_string(
    values: Mapping[str, Any],
    name: str,
    default: str | None,
) -> str | None:
    return require_optional_nonblank_string(
        values.get(name, default),
        label=f"Temporal {name}",
        error_type=TemporalConfigurationError,
    )


def _boolean(values: Mapping[str, Any], name: str, default: bool) -> bool:
    if name not in values:
        return default
    return require_boolean(
        values[name],
        label=f"Temporal {name}",
        error_type=TemporalConfigurationError,
    )


def _integer(values: Mapping[str, Any], name: str, default: int) -> int:
    return require_integer(
        values.get(name, default),
        label=f"Temporal {name}",
        error_type=TemporalConfigurationError,
    )


def _number(values: Mapping[str, Any], name: str, default: float) -> float:
    return require_finite_number(
        values.get(name, default),
        label=f"Temporal {name}",
        error_type=TemporalConfigurationError,
    )
