from __future__ import annotations

import pytest

from ..eval_metrics import check, summarize

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_summarize_counts_and_details() -> None:
    results = [check("a", True), check("b", False, "missed node"), check("c", True)]
    summary = summarize(results)
    assert summary["total"] == 3
    assert summary["passed"] == 2
    assert summary["failures"] == [{"name": "b", "detail": "missed node"}]


def test_check_drops_detail_on_pass() -> None:
    assert check("a", True, "irrelevant").detail == ""
