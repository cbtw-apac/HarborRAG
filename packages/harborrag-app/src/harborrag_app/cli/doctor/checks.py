"""Check results and the report that aggregates them."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from harborrag_app.cli.exit_codes import UNAVAILABLE

SERVICES_GROUP = "services"
_CONFIGURATION_FAILED = 1

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
    tenants: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(check.required and check.status == "fail" for check in self.checks)

    @property
    def exit_code(self) -> int:
        """``0`` when ready, ``6`` when only service checks failed, ``1`` for any other failure."""
        failed = [check for check in self.checks if check.required and check.status == "fail"]
        if not failed:
            return 0
        if all(check.group == SERVICES_GROUP for check in failed):
            return UNAVAILABLE
        return _CONFIGURATION_FAILED

    def as_payload(self) -> dict[str, object]:
        counts: Counter[str] = Counter(str(check.status) for check in self.checks)
        payload: dict[str, object] = {
            "checks": [check.as_dict() for check in self.checks],
            # `summary` counts every status; `ready` is the exit-code decision, which
            # ignores failures of non-required checks. Renderers must use `ready`.
            "ready": self.ok,
            "summary": {status: counts.get(status, 0) for status in _STATUSES},
            "tenants": list(self.tenants),
        }
        if self.diagnostics is not None:
            payload["diagnostics"] = self.diagnostics
        return payload
