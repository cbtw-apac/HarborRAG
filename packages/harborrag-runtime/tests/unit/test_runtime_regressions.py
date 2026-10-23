"""Runtime behaviours that contradicted their own documented contract."""

from __future__ import annotations

import time

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_engine.tools.vector_search import _meets, _results

pytestmark = [pytest.mark.unit]


class _Response:
    def __init__(self, *results: RetrievalResult) -> None:
        self.results = results


def test_a_threshold_measures_relevance_not_rank_arithmetic() -> None:
    """The settings and the output schema both say score is not thresholdable.

    On the hybrid lane it is rank fusion, so the top hit sits near 1.0 however
    poor the match. Filtering on it returned that hit whatever the quality and
    dropped good matches further down the ranking.
    """

    top_but_irrelevant = RetrievalResult("a", "text", 0.99, {}, relevance=0.20)
    lower_but_relevant = RetrievalResult("b", "text", 0.40, {}, relevance=0.95)

    kept = _results(_Response(top_but_irrelevant, lower_but_relevant), 0.8)

    assert [result["id"] for result in kept] == ["b"]


def test_a_hit_without_measured_similarity_cannot_meet_a_threshold() -> None:
    """A null relevance means nothing measured it; its score is no stand-in.

    The sparse lane's score is a squashed BM25 value -- 0.95 for an off-topic CV --
    so letting it answer a similarity threshold passed exactly what it should not.
    """

    unmeasured = RetrievalResult("a", "text", 0.9, {})

    assert not _meets(unmeasured, 0.5)
    assert _results(_Response(unmeasured), 0.5) == []
    # No threshold is no filter: unmeasured hits are still returned.
    assert [r["id"] for r in _results(_Response(unmeasured), 0.0)] == ["a"]


def test_expired_handles_are_swept_without_walking_the_whole_store() -> None:
    """Entries share one TTL, so insertion order is expiry order.

    The sweep ran on every issue and scanned every entry, which at the
    hundred-thousand ceiling meant a full scan under the lock per tool call.
    """

    store = KnowledgeReferenceStore(ttl_seconds=1)
    stale = [
        store.issue("evidence", f"chunk-{index}", tenant_id="T", principal_id="P")
        for index in range(3)
    ]
    time.sleep(1.05)

    fresh = store.issue("evidence", "chunk-new", tenant_id="T", principal_id="P")

    for handle in stale:
        assert store.resolve(handle, "evidence", tenant_id="T", principal_id="P") is None
    assert store.resolve(fresh, "evidence", tenant_id="T", principal_id="P") == "chunk-new"


def test_a_live_entry_stops_the_sweep() -> None:
    """The scan must not continue past the first entry that is still good."""

    store = KnowledgeReferenceStore(ttl_seconds=60)
    first = store.issue("evidence", "chunk-1", tenant_id="T", principal_id="P")
    second = store.issue("evidence", "chunk-2", tenant_id="T", principal_id="P")

    assert store.resolve(first, "evidence", tenant_id="T", principal_id="P") == "chunk-1"
    assert store.resolve(second, "evidence", tenant_id="T", principal_id="P") == "chunk-2"
