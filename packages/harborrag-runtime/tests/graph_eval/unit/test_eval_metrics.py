from __future__ import annotations

import pytest

from harborrag_core.ingestion import KnowledgeGraphTraversal
from harborrag_engine.retrieval.graph import (
    AuthoritativeSubgraphResult,
    GraphSearchDiagnostics,
)

from ..corpus import EvalCorpus
from ..eval_metrics import check, summarize
from ..golden import StalenessCase

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_summarize_counts_and_details() -> None:
    results = [check("a", True), check("b", False, "missed node"), check("c", True)]
    summary = summarize(results)
    assert summary["total"] == 3
    assert summary["passed"] == 2
    assert summary["failures"] == [{"name": "b", "detail": "missed node"}]


def test_check_drops_detail_on_pass() -> None:
    assert check("a", True, "irrelevant").detail == ""


def _subgraph_result(stale_count: int) -> AuthoritativeSubgraphResult:
    return AuthoritativeSubgraphResult(
        graph=KnowledgeGraphTraversal(nodes=(), relations=()),
        diagnostics=GraphSearchDiagnostics(
            candidate_count=0,
            accepted_count=0,
            stale_count=stale_count,
            unpublished_count=0,
            projection_truncated=False,
        ),
    )


@pytest.mark.parametrize(
    ("expect_stale_rejections", "stale_count", "passed"),
    [(True, 1, True), (True, 0, False), (False, 0, True), (False, 1, False)],
)
def test_staleness_case_requires_stale_count_to_match_expectation(
    corpus: EvalCorpus, expect_stale_rejections: bool, stale_count: int, passed: bool
) -> None:
    """Unexpected stale rejections fail, not just missing expected ones."""

    case = StalenessCase(
        name="stale",
        seed_doc="runbook",
        stale_docs=frozenset(),
        max_depth=1,
        max_nodes=10,
        forbidden_docs=frozenset(),
        expect_stale_rejections=expect_stale_rejections,
    )
    assert case.evaluate(_subgraph_result(stale_count), corpus).passed is passed
