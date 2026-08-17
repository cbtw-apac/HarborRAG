from __future__ import annotations

import pytest

from ..health.diffing import diff_reports

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _report(**overrides) -> dict:
    payload: dict = {
        "tenant_id": "tenant-1",
        "node_count": 10,
        "relation_count": 9,
        "relations_by_type": {"contains": 5, "supports": 4},
        "signature_census": {"Chunk supports Structure": 4},
        "node_keys": ["a", "b", "c", "d"],
        "relation_ids": ["r1", "r2", "r3"],
        "placeholder_count": 13,
    }
    payload.update(overrides)
    return payload


def test_identical_reports_diff_clean() -> None:
    diff = diff_reports(_report(), _report())
    assert diff.node_jaccard == 1.0
    assert diff.relation_jaccard == 1.0
    assert diff.added_signatures == ()
    assert diff.removed_signatures == ()
    assert diff.gate_failures(1.0, 1.0, allow_new_signatures=False) == ()


def test_jaccard_measures_identity_churn() -> None:
    diff = diff_reports(_report(), _report(node_keys=["a", "b", "x", "y"]))
    assert diff.node_jaccard == 2 / 6
    assert diff.gate_failures(0.5, 0.0, allow_new_signatures=True) != ()


def test_new_signature_is_gated_unless_allowed() -> None:
    current = _report(signature_census={"Chunk supports Structure": 4, "Tenant links_to Chunk": 1})
    diff = diff_reports(_report(), current)
    assert diff.added_signatures == ("Tenant links_to Chunk",)
    assert diff.gate_failures(0.0, 0.0, allow_new_signatures=False) != ()
    assert diff.gate_failures(0.0, 0.0, allow_new_signatures=True) == ()


def test_missing_identities_disable_jaccard() -> None:
    baseline, current = _report(), _report()
    del baseline["node_keys"], baseline["relation_ids"]
    diff = diff_reports(baseline, current)
    assert diff.node_jaccard is None
    assert diff.gate_failures(0.9, 0.9, allow_new_signatures=False) == ()


def test_placeholder_drift_fails_the_default_gate() -> None:
    # A real page flipped to placeholder live while every identity and census key stayed
    # byte-identical: this delta was the only report value that moved.
    diff = diff_reports(_report(), _report(placeholder_count=14))
    assert diff.placeholder_count_delta == (13, 14)
    assert diff.node_jaccard == 1.0
    assert diff.gate_failures(1.0, 1.0, allow_new_signatures=False) != ()
    # The census-only opt-out silences it, like the missing-identity failure.
    assert diff.gate_failures(0.0, 0.0, allow_new_signatures=False) == ()


def test_equal_placeholder_counts_diff_clean() -> None:
    diff = diff_reports(_report(), _report())
    assert diff.placeholder_count_delta == (13, 13)
    assert diff.gate_failures(1.0, 1.0, allow_new_signatures=False) == ()


def test_census_deltas_reported() -> None:
    diff = diff_reports(_report(), _report(relations_by_type={"contains": 7, "supports": 4}))
    assert diff.relation_count_deltas["contains"] == (5, 7)
