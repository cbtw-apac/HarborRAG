"""Gate the corpus's health report against the committed baseline in ``health/baselines/``.

The corpus is deterministic, so its report is too: any change to the projection, a
projector, the chunking config or a fixture moves that file, and the JSON diff is the
change's blast radius -- node keys, edge signatures, censuses and all.

Regenerate after an *intended* change and review the diff before committing it:

    HARBORRAG_UPDATE_BASELINE=1 .venv/bin/pytest \\
        packages/harborrag-runtime/tests/graph_eval/unit/test_health_baseline.py

CI must never set that variable -- it rewrites the baseline to whatever the run produced,
which makes the assertions below vacuous.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ..corpus import TENANT_ID, EvalCorpus
from ..health.corpus_census import corpus_health_entry
from ..health.diffing import diff_reports

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

# The baseline is health data, so it stays with the health library rather than moving
# here with its test.
BASELINES = Path(__file__).resolve().parents[1] / "health" / "baselines"
BASELINE = BASELINES / f"{TENANT_ID}.json"


def _stored() -> dict[str, object]:
    """The one report in the baseline file.

    Stored as a list of reports, not a bare report: that is what
    ``graph_health.py --output`` writes, so one file format serves both, and
    ``graph_diff.py`` can be pointed straight at this baseline.
    """

    # Named explicitly: TENANT_ID picks the filename, so an HARBORRAG_EVAL_TENANT_ID
    # override (env or env/.env.database) otherwise surfaces as a bare FileNotFoundError.
    assert BASELINE.exists(), (
        f"no committed baseline {BASELINE.name} -- HARBORRAG_EVAL_TENANT_ID is {TENANT_ID!r}"
    )
    reports = json.loads(BASELINE.read_text())
    assert len(reports) == 1, f"expected one report in {BASELINE.name}, got {len(reports)}"
    return reports[0]


def test_corpus_health_matches_the_committed_baseline(corpus: EvalCorpus) -> None:
    current = corpus_health_entry(corpus)
    # Before anything is compared, and before a regeneration can bake it in: gate_failures
    # is part of the serialized report, so equality alone would compare failures against
    # failures and hold CI green on a corpus the health gate is meant to reject.
    assert not current["gate_failures"]
    if os.environ.get("HARBORRAG_UPDATE_BASELINE"):
        BASELINE.write_text(json.dumps([current], indent=2, sort_keys=True) + "\n")
    baseline = _stored()
    diff = diff_reports(baseline, current)
    # The diff names *which* identity, signature or placeholder count moved, so it is the
    # assertion that reports a regression legibly; equality is the backstop for everything
    # diff_reports only reports rather than gates (entity types, hubs, components).
    assert diff.gate_failures(1.0, 1.0, allow_new_signatures=False) == (), diff.as_dict()
    assert current == baseline


def test_baseline_carries_the_identities_the_diff_gates_on() -> None:
    """Without these keys diff_reports disables both Jaccard gates and passes vacuously."""

    baseline = _stored()
    assert baseline["node_keys"] and baseline["relation_ids"]
    assert baseline["tenant_id"] == TENANT_ID
