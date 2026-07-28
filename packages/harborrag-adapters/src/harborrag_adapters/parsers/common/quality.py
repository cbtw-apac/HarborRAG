"""Generic quality primitives reusable by multiple parser families."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """A normalized score and acceptance decision from a family evaluator."""

    score: float
    accepted: bool
    message: str | None = None
