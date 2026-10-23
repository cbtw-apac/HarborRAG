"""A provider request must not outlive the durable slot that admitted it."""

from datetime import UTC, datetime

from harborrag_core.topology.budget import BudgetAdmission


def reservation_seconds(admission: BudgetAdmission) -> float:
    reservation = admission.reservation
    if not admission.admitted or reservation is None:
        raise ValueError("admitted model work requires a durable reservation")
    remaining = (reservation.expires_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("model reservation expired before dispatch")
    # Leave a bounded margin for cooperative cancellation and clock/transport jitter.
    return remaining - min(1.0, remaining * 0.1)
