"""The connection test chain: reachable, auth, permissions and preview."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
    RateLimitError,
)
from harborrag_core.domain.source import SourceRecord
from harborrag_runtime.ingestion.connection_checks import (
    run_connection_checks,
    timed_out_report,
)


class _Connector:
    """Connector double whose connect and discover can fail on demand."""

    def __init__(
        self, *, connect_error: Exception | None = None, discover_error: Exception | None = None
    ) -> None:
        self._connect_error = connect_error
        self._discover_error = discover_error
        self.closed = False

    def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error

    def discover(self, _query: object) -> Iterator[SourceRecord]:
        if self._discover_error is not None:
            raise self._discover_error
        for index in range(9):
            yield SourceRecord(
                id=f"ENG-{index}",
                source_type="jira",
                locator=f"https://jira/ENG-{index}",
                metadata={"title": f"Issue {index}"},
            )

    def close(self) -> None:
        self.closed = True


def _run(**errors: Exception) -> tuple[dict[str, Any], _Connector]:
    connector = _Connector(**errors)
    return run_connection_checks(cast(Any, connector)).as_dict(), connector


def test_a_healthy_source_passes_every_check_and_previews_five_items() -> None:
    report, connector = _run()

    assert report["ok"] is True
    assert [check["name"] for check in report["checks"]] == [
        "schema",
        "reachable",
        "auth",
        "permissions",
        "preview",
    ]
    assert report["error_category"] is None
    assert report["preview"]["sample"][0] == {"ref": "ENG-0", "title": "Issue 0"}
    assert len(report["preview"]["sample"]) == 5
    assert report["preview"]["estimated_items"] is None
    assert connector.closed is True


@pytest.mark.parametrize(
    ("errors", "failed", "category"),
    [
        ({"connect_error": AuthenticationError("bad", status_code=401)}, "auth", "auth_invalid"),
        ({"connect_error": FetchError("down")}, "reachable", "unreachable"),
        ({"connect_error": RateLimitError("slow down")}, "reachable", "rate_limited"),
        (
            {"discover_error": AuthorizationError("no", status_code=403)},
            "permissions",
            "forbidden",
        ),
    ],
)
def test_the_first_failing_check_stops_the_chain(
    errors: dict[str, Exception], failed: str, category: str
) -> None:
    report, connector = _run(**errors)

    assert report["ok"] is False
    assert report["checks"][-1]["name"] == failed and report["checks"][-1]["ok"] is False
    assert report["error_category"] == category
    assert report["preview"]["sample"] == []
    assert connector.closed is True


def test_failure_messages_carry_the_status_but_never_the_source_text() -> None:
    report, _ = _run(connect_error=AuthenticationError("token=ATATT-secret", status_code=401))

    message = report["checks"][-1]["message"]
    assert message.startswith("401 from the source")
    assert "ATATT" not in message


def test_a_timeout_reports_the_source_unreachable() -> None:
    report = timed_out_report().as_dict()

    assert report["ok"] is False
    assert report["error_category"] == "unreachable"
    assert [check["name"] for check in report["checks"]] == ["schema", "reachable"]
