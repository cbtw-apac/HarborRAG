"""Check results and the report that aggregates them."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

CheckStatus = Literal["ok", "fail", "warn", "skip"]
_STATUSES: tuple[CheckStatus, ...] = ("ok", "fail", "warn", "skip")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    group: str
    status: CheckStatus
    detail: str
    hint: str = ""
    required: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "group": self.group,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[Check, ...] = field(default_factory=tuple)
    # ``service.health()`` diagnostics, kept for callers that read ``data.diagnostics``.
    diagnostics: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        return not any(check.required and check.status == "fail" for check in self.checks)

    def as_payload(self) -> dict[str, object]:
        counts: Counter[str] = Counter(str(check.status) for check in self.checks)
        payload: dict[str, object] = {
            "checks": [check.as_dict() for check in self.checks],
            "summary": {status: counts.get(status, 0) for status in _STATUSES},
        }
        if self.diagnostics is not None:
            payload["diagnostics"] = self.diagnostics
        return payload


__all__ = ["Check", "CheckStatus", "DoctorReport"]
