"""Explicit Temporal retry and timeout policy by activity class."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio.common import RetryPolicy

from harborrag_runtime.errors import RuntimeConfigurationError
from harborrag_runtime.temporal.task_queues import ActivityClass

_CONFIGURATION_ERRORS = (
    "RuntimeConfigurationError",
    "ConnectorConfigurationError",
    "ParserConfigurationError",
    "ChunkValidationError",
    "ValueError",
)


@dataclass(frozen=True, slots=True)
class ActivityPolicy:
    """Serializable retry, execution, and heartbeat settings."""

    start_to_close_seconds: int
    heartbeat_seconds: int
    maximum_attempts: int
    initial_backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 60.0
    backoff_coefficient: float = 2.0
    non_retryable_error_types: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        positive = (
            self.start_to_close_seconds,
            self.heartbeat_seconds,
            self.maximum_attempts,
            self.initial_backoff_seconds,
            self.maximum_backoff_seconds,
            self.backoff_coefficient,
        )
        if any(value <= 0 for value in positive):
            raise RuntimeConfigurationError("Activity timeout and retry values must be positive")
        if self.heartbeat_seconds >= self.start_to_close_seconds:
            raise RuntimeConfigurationError(
                "Activity heartbeat timeout must be shorter than start-to-close timeout"
            )

    @property
    def start_to_close_timeout(self) -> timedelta:
        return timedelta(seconds=self.start_to_close_seconds)

    @property
    def heartbeat_timeout(self) -> timedelta:
        return timedelta(seconds=self.heartbeat_seconds)

    def temporal_retry_policy(self) -> RetryPolicy:
        """Convert the HarborRAG-owned policy to the Temporal SDK contract."""

        return RetryPolicy(
            initial_interval=timedelta(seconds=self.initial_backoff_seconds),
            maximum_interval=timedelta(seconds=self.maximum_backoff_seconds),
            backoff_coefficient=self.backoff_coefficient,
            maximum_attempts=self.maximum_attempts,
            non_retryable_error_types=self.non_retryable_error_types,
        )


@dataclass(frozen=True, slots=True)
class ActivityRetryConfig:
    """Policies deliberately differ across source, compute, and storage work."""

    discovery: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=300,
        heartbeat_seconds=30,
        maximum_attempts=6,
        maximum_backoff_seconds=120,
        non_retryable_error_types=("AuthenticationError", *_CONFIGURATION_ERRORS),
    )
    connector: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=900,
        heartbeat_seconds=45,
        maximum_attempts=7,
        maximum_backoff_seconds=180,
        non_retryable_error_types=("AuthenticationError", *_CONFIGURATION_ERRORS),
    )
    parser: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=1800,
        heartbeat_seconds=60,
        maximum_attempts=3,
        maximum_backoff_seconds=30,
        non_retryable_error_types=(
            "ParseError",
            "UnsupportedFormatError",
            "EncryptedPdfError",
            *_CONFIGURATION_ERRORS,
        ),
    )
    ocr: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=3600,
        heartbeat_seconds=60,
        maximum_attempts=4,
        maximum_backoff_seconds=120,
        non_retryable_error_types=("EncryptedPdfError", *_CONFIGURATION_ERRORS),
    )
    chunking: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=900,
        heartbeat_seconds=45,
        maximum_attempts=3,
        maximum_backoff_seconds=30,
        non_retryable_error_types=_CONFIGURATION_ERRORS,
    )
    indexing: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=3600,
        heartbeat_seconds=45,
        maximum_attempts=8,
        maximum_backoff_seconds=240,
        non_retryable_error_types=("InvalidIndexingSchemaError", *_CONFIGURATION_ERRORS),
    )
    validation: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=600,
        heartbeat_seconds=30,
        maximum_attempts=4,
        maximum_backoff_seconds=60,
        non_retryable_error_types=_CONFIGURATION_ERRORS,
    )
    maintenance: ActivityPolicy = ActivityPolicy(
        start_to_close_seconds=1800,
        heartbeat_seconds=60,
        maximum_attempts=8,
        maximum_backoff_seconds=240,
        non_retryable_error_types=_CONFIGURATION_ERRORS,
    )

    def for_class(self, activity_class: ActivityClass) -> ActivityPolicy:
        """Select a policy without scattering retry configuration in workflows."""

        return {
            ActivityClass.DISCOVERY: self.discovery,
            ActivityClass.CONNECTOR: self.connector,
            ActivityClass.PARSER: self.parser,
            ActivityClass.OCR: self.ocr,
            ActivityClass.CHUNKING: self.chunking,
            ActivityClass.INDEXING: self.indexing,
            ActivityClass.VALIDATION: self.validation,
            ActivityClass.MAINTENANCE: self.maintenance,
        }[activity_class]
