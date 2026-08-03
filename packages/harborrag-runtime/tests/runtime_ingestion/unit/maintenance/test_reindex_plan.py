"""Connector-free reindex planning policy."""

from __future__ import annotations

import pytest

from harborrag_core.ingestion import ProcessingProfile
from harborrag_runtime.ingestion import ReindexPlan


def _profile(**updates: str) -> ProcessingProfile:
    return ProcessingProfile(
        parser_profile="parser-v1",
        normalizer_version="canonical-v1",
        chunk_strategy="chunks-v1",
        dense_encoder_profile="dense-v1",
        sparse_encoder_profile="sparse-v1",
        graph_projection_version="graph-v1",
        vector_projection_schema="payload-v1",
    ).model_copy(update=updates)


@pytest.mark.parametrize(
    ("updates", "expected"),
    (
        (
            {"dense_encoder_profile": "dense-v2"},
            (False, True, False, False, True),
        ),
        (
            {"sparse_encoder_profile": "sparse-v2"},
            (False, False, True, False, True),
        ),
        (
            {"chunk_strategy": "chunks-v2"},
            (True, True, True, True, True),
        ),
        (
            {"graph_projection_version": "graph-v2"},
            (False, False, False, True, False),
        ),
        (
            {"vector_projection_schema": "payload-v2"},
            (False, False, False, False, True),
        ),
    ),
)
def test_reindex_plan_selects_only_stale_lanes(
    updates: dict[str, str],
    expected: tuple[bool, bool, bool, bool, bool],
) -> None:
    plan = ReindexPlan.between(_profile(), _profile(**updates))

    assert (
        plan.rebuild_chunks,
        plan.regenerate_dense,
        plan.regenerate_sparse,
        plan.rebuild_graph,
        plan.rebuild_vector_projection,
    ) == expected


def test_connector_free_plan_rejects_parser_and_normalizer_changes() -> None:
    with pytest.raises(ValueError, match="raw-artifact replay"):
        ReindexPlan.between(_profile(), _profile(parser_profile="parser-v2"))
    with pytest.raises(ValueError, match="raw-artifact replay"):
        ReindexPlan.between(_profile(), _profile(normalizer_version="canonical-v2"))
