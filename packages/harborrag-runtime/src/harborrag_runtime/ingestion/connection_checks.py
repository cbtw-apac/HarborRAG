"""Check a connection against its source: reachability, credentials, permissions, preview.

Handles:
- a chain of check steps, each recording its checks and stopping the chain on failure
- mapping connector errors to the spec's ``error_category`` without leaking source text
- a bounded preview of up to five items

Does NOT validate the connection's schema or store anything.
"""

from __future__ import annotations

import contextlib
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Literal

from harborrag_adapters.connectors.exceptions import RateLimitError
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.policies.network import BlockedDestinationError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion.errors import (
    SourceAuthenticationError,
    SourceAuthorizationError,
)
from harborrag_runtime.connections.secrets import (
    EnvSecretUnavailableError,
    SecretUnavailableError,
)

CheckName = Literal["schema", "auth", "reachable", "permissions", "preview"]
ErrorCategory = Literal[
    "auth_invalid",
    "auth_expired",
    "forbidden",
    "unreachable",
    "rate_limited",
    "secret_unavailable",
]

_SOURCE_CHECKS: tuple[CheckName, ...] = ("reachable", "auth", "permissions", "preview")
_NOT_PERMITTED = frozenset({403, 404})

PREVIEW_SIZE = 5
ENV_SECRET_MESSAGE = "the secret is read from the worker's environment; the API cannot check it"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One named check and why it failed, if it did."""

    name: CheckName
    ok: bool
    message: str | None = None


@dataclass(slots=True)
class ConnectionTestReport:
    """The spec's ``ConnectionTest`` result, built up by the chain."""

    checks: list[CheckResult] = field(default_factory=list)
    error_category: ErrorCategory | None = None
    sample: list[dict[str, str]] = field(default_factory=list)
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        """True when every recorded check passed."""
        return all(check.ok for check in self.checks)

    def passed(self, *names: CheckName) -> None:
        """Record checks that succeeded."""
        self.checks.extend(CheckResult(name, True) for name in names)

    def failed(self, name: CheckName, category: ErrorCategory, message: str) -> None:
        """Record the check that failed and the category clients branch on."""
        self.checks.append(CheckResult(name, False, message))
        self.error_category = category

    def secret_unavailable(self, error: SecretUnavailableError) -> None:
        """Record an unreadable secret; an ``env:`` one lives in the worker, not the API."""
        message = ENV_SECRET_MESSAGE if isinstance(error, EnvSecretUnavailableError) else str(error)
        self.failed("auth", "secret_unavailable", message)

    def as_dict(self) -> dict[str, Any]:
        """Return the response body."""
        return {
            "ok": self.ok,
            "checks": [
                {"name": check.name, "ok": check.ok, "message": check.message}
                for check in self.checks
            ],
            "error_category": self.error_category,
            "preview": {"estimated_items": None, "sample": list(self.sample)},
            "latency_ms": self.latency_ms,
        }


class CheckStep(ABC):
    """One link of the chain; it records its checks and says whether to go on."""

    def __init__(self, successor: CheckStep | None = None) -> None:
        self._successor = successor

    def handle(self, connector: HarborConnector, report: ConnectionTestReport) -> None:
        """Run this step, then the next one unless this step failed."""
        try:
            self.run(connector, report)
        except Exception as error:
            check, category, message = classify(error, default=self.default_check)
            recorded = {result.name for result in report.checks}
            if check in recorded:
                check = self.default_check
            report.passed(
                *(
                    name
                    for name in _SOURCE_CHECKS[: _SOURCE_CHECKS.index(check)]
                    if name in self.implied and name not in recorded
                )
            )
            report.failed(check, category, message)
            return
        if self._successor is not None:
            self._successor.handle(connector, report)

    implied: tuple[CheckName, ...] = ()
    """Checks a failure in this step proves passed when it fails a later check."""

    @property
    @abstractmethod
    def default_check(self) -> CheckName:
        """Check blamed for an error that names no better one."""

    @abstractmethod
    def run(self, connector: HarborConnector, report: ConnectionTestReport) -> None:
        """Do the step's work and record its passed checks."""


class ConnectStep(CheckStep):
    """Open the connector: each connector's ``connect`` calls the source with the credentials."""

    default_check: CheckName = "reachable"
    implied: tuple[CheckName, ...] = ("reachable",)

    def run(self, connector: HarborConnector, report: ConnectionTestReport) -> None:
        """Connect and record ``reachable`` and ``auth``."""
        connector.connect()
        report.passed("reachable", "auth")


class PreviewStep(CheckStep):
    """List the first items: proves read permission and shows a sample."""

    default_check: CheckName = "permissions"
    implied: tuple[CheckName, ...] = ("reachable", "auth", "permissions")

    def __init__(self, successor: CheckStep | None = None, *, size: int = PREVIEW_SIZE) -> None:
        super().__init__(successor)
        self._size = size

    def run(self, connector: HarborConnector, report: ConnectionTestReport) -> None:
        """Read up to ``size`` items and record ``permissions`` and ``preview``."""
        records: Iterator[SourceRecord] = connector.discover(ConnectorQuery(limit=self._size))
        report.sample = [_sample(record) for record in islice(records, self._size)]
        report.passed("permissions", "preview")


def run_connection_checks(connector: HarborConnector) -> ConnectionTestReport:
    """Run the chain against a built connector, after the schema check passed.

    Args:
        connector: The connector built from the values under test.

    Returns:
        The report; ``ok`` is False when any check failed.
    """
    report = ConnectionTestReport()
    report.passed("schema")
    started = time.monotonic()
    try:
        ConnectStep(PreviewStep()).handle(connector, report)
    finally:
        report.latency_ms = int((time.monotonic() - started) * 1000)
        with contextlib.suppress(Exception):
            connector.close()
    return report


def classify(error: BaseException, *, default: CheckName) -> tuple[CheckName, ErrorCategory, str]:
    """Map a connector error to the failing check, its category and a safe message.

    An error with no HTTP status means the source never answered, so it fails
    ``reachable`` whichever step raised it, as does a blocked private address,
    whose message is HarborRAG's own; 403 and 404 fail ``permissions``.
    """
    status = getattr(error, "status_code", None)
    prefix = f"{status} from the source: " if status else ""
    if isinstance(error, BlockedDestinationError):
        return "reachable", "unreachable", str(error)
    if isinstance(error, SourceAuthenticationError):
        return "auth", "auth_invalid", f"{prefix}the credentials were rejected"
    if isinstance(error, SourceAuthorizationError) or status in _NOT_PERMITTED:
        return "permissions", "forbidden", f"{prefix}the credentials cannot read this content"
    if isinstance(error, RateLimitError):
        return "reachable", "rate_limited", f"{prefix}the source is rate limiting requests"
    if isinstance(error, TimeoutError):
        return "reachable", "unreachable", "the source did not answer in time"
    check = default if status else "reachable"
    return check, "unreachable", f"{prefix}the source could not be read ({type(error).__name__})"


def timed_out_report() -> ConnectionTestReport:
    """Report for a test that ran past its deadline."""
    report = ConnectionTestReport()
    report.passed("schema")
    report.failed("reachable", "unreachable", "the source did not answer within 15 s")
    return report


def _sample(record: SourceRecord) -> dict[str, str]:
    """Reduce a discovered record to the preview's ``{ref, title}``."""
    title = record.metadata.get("title") or record.metadata.get("name") or record.locator
    return {"item_ref": record.id, "title": str(title)}
