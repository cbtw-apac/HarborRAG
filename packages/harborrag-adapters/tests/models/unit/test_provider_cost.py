"""Unit tests for the in-memory provider cost tracker."""

from __future__ import annotations

import pytest

from harborrag_adapters.models.runtime.provider_cost import InMemoryProviderCostTracker


def test_snapshot_reports_zero_for_a_provider_never_recorded() -> None:
    tracker = InMemoryProviderCostTracker()

    assert tracker.snapshot(["prov_1"]) == {"prov_1": 0.0}


def test_record_accumulates_per_provider() -> None:
    tracker = InMemoryProviderCostTracker()

    tracker.record("prov_1", 0.02)
    tracker.record("prov_1", 0.03)
    tracker.record("prov_2", 1.5)

    snapshot = tracker.snapshot(["prov_1", "prov_2", "prov_3"])
    assert snapshot["prov_1"] == pytest.approx(0.05)
    assert snapshot["prov_2"] == pytest.approx(1.5)
    assert snapshot["prov_3"] == 0.0


def test_started_at_is_set_on_construction() -> None:
    tracker = InMemoryProviderCostTracker()

    assert tracker.started_at is not None
