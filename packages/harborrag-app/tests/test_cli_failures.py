"""``harborrag failures``: list with filters and paging, retry by ids or by filter."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cli_fakes import Call, FakeOperations, serving

from harborrag_app.api.v1.ingestion.schemas import (
    FailureOut,
    FailurePage,
    FailureQuery,
    FailureRetryAccepted,
    FailureRetryFilter,
)
from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.cli.views import render

pytestmark = pytest.mark.unit

_STAMP = datetime(2026, 11, 10, tzinfo=UTC)
_JIRA = "engineering/jira-main"


def _failure(failure_id: str) -> FailureOut:
    return FailureOut(
        failure_id=failure_id,
        ingestion_id="ing_1",
        connection_ref=_JIRA,
        document_ref="DEMO-1",
        stage="interpret",
        error_category="parse_error",
        message="the parser gave up",
        attempts=1,
        retryable=True,
        resolved=False,
        created_at=_STAMP,
    )


class _PagedFailures(FakeOperations):
    """Answers each ``list_failures`` with the next page."""

    def __init__(self, pages: list[FailurePage]) -> None:
        super().__init__()
        self.pages = list(pages)

    async def list_failures(self, *args: object, **kwargs: object) -> FailurePage:  # type: ignore[override]  # the canned-result method is generic
        self.calls.append(Call("list_failures", args, dict(kwargs)))
        return self.pages.pop(0)


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOperations:
    operations = FakeOperations(
        results={
            "retry_failures": FailureRetryAccepted(
                ingestion_id="ing_2", ingestion_ids=["ing_2"], queued=2
            )
        }
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--ids", "fail_1", "--connection-ref", _JIRA],
        ["--stage", "unknown"],
    ],
)
def test_retry_needs_exactly_one_selector_as_over_http(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, _, err = _run(capsys, "failures", "retry", *argv)

    assert code == 2
    assert err
    assert fake.calls == []


def test_retry_by_ids_prints_the_new_run(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "failures", "retry", "--ids", "fail_1, fail_2", "-o", "json")

    assert code == 0
    assert json.loads(out) == {"ingestion_id": "ing_2", "ingestion_ids": ["ing_2"], "queued": 2}
    body = fake.calls[0].args[1]
    assert (body.ids, body.filter) == (["fail_1", "fail_2"], None)


def test_retry_by_filter_sends_only_the_given_fields(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _run(
        capsys, "failures", "retry", "--connection-ref", _JIRA, "--stage", "interpret"
    )

    assert code == 0
    body = fake.calls[0].args[1]
    assert (body.ids, body.filter) == (
        None,
        FailureRetryFilter(connection_ref=_JIRA, stage="interpret"),
    )


def test_list_all_sends_the_filters_and_merges_the_pages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = {"parse_error": 2}
    operations = _PagedFailures(
        [
            FailurePage(items=[_failure("fail_1")], next_cursor="c1", counts=counts),
            FailurePage(items=[_failure("fail_2")], next_cursor=None, counts=counts),
        ]
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))

    code, out, _ = _run(
        capsys,
        "failures",
        "list",
        "--connection-ref",
        _JIRA,
        "--stage",
        "interpret",
        "--resolved",
        "--include-total",
        "--all",
        "-o",
        "json",
    )

    body = json.loads(out)
    assert code == 0
    assert [item["failure_id"] for item in body["items"]] == ["fail_1", "fail_2"]
    assert (body["next_cursor"], body["counts"]) == (None, counts)
    queries = [call.args[1] for call in operations.calls]
    assert all(isinstance(query, FailureQuery) for query in queries)
    assert [query.cursor for query in queries] == [None, "c1"]
    first = queries[0]
    assert (first.connection_ref, first.stage, first.resolved, first.include_total) == (
        _JIRA,
        "interpret",
        True,
        True,
    )


def test_list_with_a_query_over_its_limit_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "failures", "list", "--q", "x" * 201)

    assert code == 2
    assert "q" in err
    assert fake.calls == []


def test_table_columns_come_from_every_row_not_only_the_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render(
        "list_failures",
        {
            "items": [
                {"failure_id": "fail_1"},
                {"failure_id": "fail_2", "connection_ref": _JIRA},
            ],
            "next_cursor": None,
        },
    )
    out = capsys.readouterr().out

    assert "connection_ref" in out
    assert _JIRA in out


def test_a_page_without_known_columns_prints_its_tree(capsys: pytest.CaptureFixture[str]) -> None:
    render("list_failures", {"items": [{"document_ref": "DEMO-1"}], "next_cursor": None})
    out = capsys.readouterr().out

    assert "No items." not in out
    assert "document_ref: DEMO-1" in out
