"""Whether the corpus still covers what the golden cases and signatures assume.

Separate from the topology suite: these assert nothing about *shape*, only that
the fixture set has not drifted out from under the cases that read it.
"""

from __future__ import annotations

import pytest

from ..corpus import CORPUS_SIGNATURES, EvalCorpus
from ..golden import PATH_CASES, STALENESS_CASES, SUBGRAPH_CASES, TRIPLET_CASES

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_every_golden_case_names_a_corpus_document(corpus: EvalCorpus) -> None:
    """`golden/` only runs live, so CI has to catch a case naming a dropped document.

    Importing the module also guards the engine result-model imports it depends on.
    """

    referenced = (
        {c.start_doc for c in PATH_CASES}
        | {c.end_doc for c in PATH_CASES}
        | {c.seed_doc for c in SUBGRAPH_CASES}
        | {c.subject_doc for c in TRIPLET_CASES}
        | {d for c in TRIPLET_CASES for d in c.expected_object_docs}
        | {c.seed_doc for c in STALENESS_CASES}
        | {d for c in STALENESS_CASES for d in c.stale_docs | c.forbidden_docs}
        | {d for c in SUBGRAPH_CASES for d in c.expected_docs | c.forbidden_docs}
    )
    assert referenced <= set(corpus.batches)


def test_corpus_exercises_full_signature_vocabulary(corpus: EvalCorpus) -> None:
    observed = {
        (kinds[r.source_node_key], r.relation_type.value, kinds[r.target_node_key])
        for batch in corpus.batches.values()
        for kinds in [{n.node_key: n.node_kind.value for n in batch.nodes}]
        for r in batch.relations
    }
    assert observed == CORPUS_SIGNATURES, (
        f"missing={sorted(CORPUS_SIGNATURES - observed)} extra={sorted(observed - CORPUS_SIGNATURES)}"
    )
