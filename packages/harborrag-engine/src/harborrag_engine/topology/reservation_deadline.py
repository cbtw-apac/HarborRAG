"""A provider request must not outlive the durable slot that admitted it."""

from datetime import UTC, datetime

from harborrag_core.topology.budget import BudgetAdmission


def reservation_seconds(admission: BudgetAdmission) -> float:
    """Return the seconds a provider call may run, leaving a margin for cancellation and jitter.

    The margin is the smaller of one second and a tenth of the remaining time.

    Args:
        admission: Budget admission whose reservation bounds the request.

    Returns:
        Remaining reservation time minus the margin.

    Raises:
        ValueError: The admission is not admitted or carries no reservation.
        TimeoutError: The reservation already expired.
    """
    reservation = admission.reservation
    if not admission.admitted or reservation is None:
        raise ValueError("admitted model work requires a durable reservation")
    remaining = (reservation.expires_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("model reservation expired before dispatch")
    return remaining - min(1.0, remaining * 0.1)
