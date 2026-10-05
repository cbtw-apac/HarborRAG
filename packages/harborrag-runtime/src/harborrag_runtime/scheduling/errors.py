"""Schedule management errors; messages only interpolate caller-supplied identifiers."""

from __future__ import annotations

from harborrag_runtime.errors import WorkflowOperationError


class ScheduleValidationError(ValueError):
    """A schedule definition or request breaks a schedule rule."""


class InvalidCronError(ScheduleValidationError):
    pass


class InvalidTimezoneError(ScheduleValidationError):
    pass


class UnsupportedWorkflowError(ScheduleValidationError):
    pass


class ScheduleConflictError(RuntimeError):
    """The request conflicts with an existing schedule."""


class DuplicateScheduleError(ScheduleConflictError):
    pass


class ScheduleOwnershipError(ScheduleConflictError):
    """The schedule is owned by version-controlled config and cannot be changed here."""


class ScheduleNotFoundError(WorkflowOperationError):
    pass


__all__ = [
    "DuplicateScheduleError",
    "InvalidCronError",
    "InvalidTimezoneError",
    "ScheduleConflictError",
    "ScheduleNotFoundError",
    "ScheduleOwnershipError",
    "ScheduleValidationError",
    "UnsupportedWorkflowError",
]
