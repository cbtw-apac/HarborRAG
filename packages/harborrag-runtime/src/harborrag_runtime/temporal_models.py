"""Replay-safe Temporal routing, retry and timeout configuration models.

Every model here travels inside workflow inputs, so a new field needs a default
that makes inputs written before it decode to the same activity commands.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Literal

from harborrag_core.config_store.registry import (
    MAX_ACTIVITY_TIMEOUT_SECONDS,
    MIN_ACTIVITY_TIMEOUT_SECONDS,
)
from harborrag_core.ingestion.run_contracts import RunSettings
from harborrag_runtime.errors import RuntimeConfigurationError

type TaskQueueRole = Literal["discovery", "transform", "io", "parser", "model", "index"]


@dataclass(frozen=True, slots=True)
class TaskQueueConfig:
    """Stable queue routing captured in each workflow input.

    Attributes:
        discovery: Queue for source discovery, finalization and bookkeeping.
        transform: Queue for CPU-bound transforms such as chunking.
        io: Queue for fetching and storing documents.
        parser: Queue for document parsing.
        model: Queue for model calls such as embedding.
        index: Queue for vector and graph writes and publishing.
    """

    discovery: str = "harborrag-discovery"
    transform: str = "harborrag-transform"
    io: str = "harborrag-io"
    parser: str = "harborrag-parser"
    model: str = "harborrag-model"
    index: str = "harborrag-index"

    def __post_init__(self) -> None:
        """Refuse blank, padded or repeated queue names."""
        values = self.as_tuple()
        if any(not value.strip() or value != value.strip() for value in values):
            raise RuntimeConfigurationError(
                "Temporal task-queue names must be non-empty without outer whitespace"
            )
        if len(set(values)) != len(values):
            raise RuntimeConfigurationError("Temporal task-queue names must be unique")

    def as_tuple(self) -> tuple[str, ...]:
        """Return every queue name in field order."""
        return (
            self.discovery,
            self.transform,
            self.io,
            self.parser,
            self.model,
            self.index,
        )

    def for_role(self, role: TaskQueueRole) -> str:
        """Resolve a validated logical queue role to its configured name."""
        match role:
            case "discovery":
                return self.discovery
            case "transform":
                return self.transform
            case "io":
                return self.io
            case "parser":
                return self.parser
            case "model":
                return self.model
            case "index":
                return self.index
            case _:
                raise RuntimeConfigurationError(f"Unknown Temporal task-queue role: {role!r}")


@dataclass(frozen=True, slots=True)
class RetryPolicyConfig:
    """Serializable Temporal activity retry policy.

    Attributes:
        initial_interval_seconds: Wait before the first retry.
        backoff_coefficient: Factor each wait grows by, at least 1.
        maximum_interval_seconds: Longest wait between attempts.
        maximum_attempts: Attempts in total, the first one included.
    """

    initial_interval_seconds: float = 2.0
    backoff_coefficient: float = 2.0
    maximum_interval_seconds: float = 60.0
    maximum_attempts: int = 8

    def __post_init__(self) -> None:
        """Refuse non-finite or non-positive intervals and attempts below one."""
        if (
            not isfinite(self.initial_interval_seconds)
            or not isfinite(self.backoff_coefficient)
            or not isfinite(self.maximum_interval_seconds)
            or self.initial_interval_seconds <= 0
            or self.maximum_interval_seconds <= 0
            or self.maximum_attempts < 1
            or self.backoff_coefficient < 1
            or self.maximum_interval_seconds < self.initial_interval_seconds
        ):
            raise RuntimeConfigurationError(
                "Temporal retry intervals and attempts must be positive, backoff must be "
                "at least 1, and maximum interval must not be shorter than initial interval"
            )


@dataclass(frozen=True, slots=True)
class ActivityRetryConfig:
    """Retry budgets for source-level and document-level activities.

    Attributes:
        discovery: Policy of discovery, finalization and bookkeeping activities.
        document: Policy of each document's activities.
    """

    discovery: RetryPolicyConfig = RetryPolicyConfig()
    document: RetryPolicyConfig = RetryPolicyConfig(
        maximum_interval_seconds=300.0,
        maximum_attempts=12,
    )


_MAX_ACTIVITY_TIMEOUT_SECONDS = 7 * 24 * 3600
"""A week: long enough for any source this runtime is sized for, short enough
that a typo cannot park an activity for good."""


@dataclass(frozen=True, slots=True)
class ActivityTimeoutConfig:
    """Per-attempt wall-clock budgets of the source-level activities.

    Discovery and finalization scale with the size of the source, so a fixed
    budget that fits a few hundred documents cuts a large source off on every
    attempt. Both heartbeat, so a dead worker is still noticed within the
    heartbeat timeout whatever these are set to. The defaults are the budgets
    every run used before they were configurable, which keeps runs started
    before this field existed replaying the same commands.

    Attributes:
        discovery_seconds: Budget of one discovery attempt, 60 seconds to 7 days.
        finalization_seconds: Budget of one finalization attempt, 60 seconds to 7 days.
        document_seconds: A run's ``ingestion.activity_timeout``, applied to every
            document activity, 30 seconds to 1 hour. ``None`` keeps each document
            activity's own budget.
    """

    discovery_seconds: int = 1_800
    finalization_seconds: int = 900
    document_seconds: int | None = None

    def __post_init__(self) -> None:
        """Refuse a budget outside its range."""
        if any(
            isinstance(value, bool) or not 60 <= value <= _MAX_ACTIVITY_TIMEOUT_SECONDS
            for value in (self.discovery_seconds, self.finalization_seconds)
        ):
            raise RuntimeConfigurationError(
                "Temporal activity timeouts must be between 60 seconds and 7 days"
            )
        if self.document_seconds is not None and (
            isinstance(self.document_seconds, bool)
            or not MIN_ACTIVITY_TIMEOUT_SECONDS
            <= self.document_seconds
            <= MAX_ACTIVITY_TIMEOUT_SECONDS
        ):
            raise RuntimeConfigurationError(
                "The document activity timeout must be between 30 seconds and 1 hour"
            )


@dataclass(frozen=True, slots=True)
class TemporalWorkflowOptions:
    """Replay-stable orchestration policy embedded in workflow inputs.

    Attributes:
        task_queues: Queue routing of every activity.
        retries: Retry policies of source-level and document-level activities.
        timeouts: Per-attempt budgets of source-level and document activities.
    """

    task_queues: TaskQueueConfig = TaskQueueConfig()
    retries: ActivityRetryConfig = ActivityRetryConfig()
    timeouts: ActivityTimeoutConfig = ActivityTimeoutConfig()
    native_pause: bool = False

    def for_run(self, run_settings: RunSettings | None) -> TemporalWorkflowOptions:
        """Return these options with a run's pinned activity timeout and attempts applied.

        Args:
            run_settings: The run's pinned settings. ``None``, or a field it leaves
                unset, keeps the deployment's value.

        Returns:
            These options, or a copy whose document activities use the run's
            ``ingestion.activity_timeout`` and ``ingestion.max_attempts``.
        """
        if run_settings is None:
            return self
        options = self
        if run_settings.activity_timeout_seconds is not None:
            options = replace(
                options,
                timeouts=replace(
                    options.timeouts, document_seconds=run_settings.activity_timeout_seconds
                ),
            )
        if run_settings.max_attempts is not None:
            options = replace(
                options,
                retries=replace(
                    options.retries,
                    document=replace(
                        options.retries.document, maximum_attempts=run_settings.max_attempts
                    ),
                ),
            )
        return options
