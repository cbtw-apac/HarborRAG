"""Errors raised only at the runtime and Temporal boundary."""

from __future__ import annotations


class RuntimeConfigurationError(ValueError):
    """Raised when runtime configuration is internally inconsistent."""


class RuntimeConnectionError(ConnectionError):
    """Raised when the runtime cannot connect to Temporal."""


class WorkflowSubmissionError(RuntimeError):
    """Raised when an ingestion workflow cannot be submitted."""


class WorkflowOperationError(RuntimeError):
    """Raised when a query, signal, update, or cancellation fails."""


class WorkerStartupError(RuntimeError):
    """Raised when a runtime worker cannot be constructed or started."""
