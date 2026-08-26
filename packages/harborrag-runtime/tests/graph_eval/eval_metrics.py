"""Exact-match scoring for retrieval eval cases over the deterministic corpus."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    passed: bool
    detail: str


def check(name: str, passed: bool, detail: str = "") -> CaseResult:
    return CaseResult(name=name, passed=passed, detail=detail if not passed else "")


def summarize(results: Sequence[CaseResult]) -> dict[str, object]:
    failures = [result for result in results if not result.passed]
    return {
        "total": len(results),
        "passed": len(results) - len(failures),
        "failures": [{"name": item.name, "detail": item.detail} for item in failures],
    }
