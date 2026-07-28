"""Errors raised only at the runtime and Temporal boundary."""

from __future__ import annotations


class RuntimeConfigurationError(ValueError):
    """Raised when runtime configuration is internally inconsistent."""


class RuntimeConnectionError(ConnectionError):
    """Raised when the runtime cannot connect to Temporal."""


class WorkflowSubmissionError(RuntimeError):
    """Raised when an ingestion workflow cannot be submitted."""


class WorkflowRunAlreadyStartedError(WorkflowSubmissionError):
    """Raised when the requested run ID is already in use.

    Separated from the general submission failure so transports can answer a
    reused run ID with "conflict" rather than reporting an upstream fault. Named
    distinctly from ``temporalio.exceptions.WorkflowAlreadyStartedError``, which
    it wraps, so both can be referenced in the same module.
    """


class WorkflowOperationError(RuntimeError):
    """Raised when a query, signal, update, or cancellation fails."""


class WorkflowNotFoundError(WorkflowOperationError):
    """Raised when the addressed ingestion run does not exist in Temporal.

    Separated from the general operation failure so transports can answer a
    missing run with "not found" instead of reporting an upstream fault.
    """


class WorkflowNotRetryableError(WorkflowOperationError):
    """Raised when a retry names artifacts the run cannot retry.

    The run workflow only checkpoints failed and quarantined artifacts, and
    discards retry requests for anything else without a trace. Refusing those
    up front keeps a no-op from being reported as a successful retry.
    """


class WorkflowNotRunningError(WorkflowOperationError):
    """Raised when a control action addresses a run that has already finished.

    Temporal answers NOT_FOUND for signals sent to a closed execution, which
    is indistinguishable from a genuinely unknown run even though queries still
    succeed against it. This names the difference so operators are not sent
    looking for a run that plainly exists.
    """


class WorkerStartupError(RuntimeError):
    """Raised when a runtime worker cannot be constructed or started."""
