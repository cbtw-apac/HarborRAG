"""Replay-safe Temporal routing and retry configuration models."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

from harborrag_runtime.errors import RuntimeConfigurationError

type TaskQueueRole = Literal["discovery", "transform", "io", "parser", "model", "index"]


@dataclass(frozen=True, slots=True)
class TaskQueueConfig:
    """Stable queue routing captured in each workflow input."""

    discovery: str = "harborrag-discovery"
    transform: str = "harborrag-transform"
    io: str = "harborrag-io"
    parser: str = "harborrag-parser"
    model: str = "harborrag-model"
    index: str = "harborrag-index"

    def __post_init__(self) -> None:
        values = self.as_tuple()
        if any(not value.strip() or value != value.strip() for value in values):
            raise RuntimeConfigurationError(
                "Temporal task-queue names must be non-empty without outer whitespace"
            )
        if len(set(values)) != len(values):
            raise RuntimeConfigurationError("Temporal task-queue names must be unique")

    def as_tuple(self) -> tuple[str, ...]:
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
    """Serializable Temporal activity retry policy."""

    initial_interval_seconds: float = 2.0
    backoff_coefficient: float = 2.0
    maximum_interval_seconds: float = 60.0
    maximum_attempts: int = 8

    def __post_init__(self) -> None:
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
    """Retry budgets for source-level and document-level activities."""

    discovery: RetryPolicyConfig = RetryPolicyConfig()
    document: RetryPolicyConfig = RetryPolicyConfig(
        maximum_interval_seconds=300.0,
        maximum_attempts=12,
    )


# A week: long enough for any source this runtime is sized for, short enough
# that a typo cannot park an activity for good.
_MAX_ACTIVITY_TIMEOUT_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class ActivityTimeoutConfig:
    """Per-attempt wall-clock budgets of the source-level activities.

    Discovery and finalization scale with the size of the source, so a fixed
    budget that fits a few hundred documents cuts a large source off on every
    attempt. Both heartbeat, so a dead worker is still noticed within the
    heartbeat timeout whatever these are set to. The defaults are the budgets
    every run used before they were configurable, which keeps runs started
    before this field existed replaying the same commands.
    """

    discovery_seconds: int = 1_800
    finalization_seconds: int = 900

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not 60 <= value <= _MAX_ACTIVITY_TIMEOUT_SECONDS
            for value in (self.discovery_seconds, self.finalization_seconds)
        ):
            raise RuntimeConfigurationError(
                "Temporal activity timeouts must be between 60 seconds and 7 days"
            )


@dataclass(frozen=True, slots=True)
class TemporalWorkflowOptions:
    """Replay-stable orchestration policy embedded in workflow inputs."""

    task_queues: TaskQueueConfig = TaskQueueConfig()
    retries: ActivityRetryConfig = ActivityRetryConfig()
    timeouts: ActivityTimeoutConfig = ActivityTimeoutConfig()
