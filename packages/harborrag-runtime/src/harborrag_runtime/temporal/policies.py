from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

from harborrag_runtime.temporal_models import RetryPolicyConfig


def temporal_retry_policy(config: RetryPolicyConfig) -> RetryPolicy:
    """Convert replay-stable YAML values to the Temporal SDK policy."""

    return RetryPolicy(
        initial_interval=timedelta(seconds=config.initial_interval_seconds),
        backoff_coefficient=config.backoff_coefficient,
        maximum_interval=timedelta(seconds=config.maximum_interval_seconds),
        maximum_attempts=config.maximum_attempts,
    )
