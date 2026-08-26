from __future__ import annotations

import pytest

from ..corpus import EvalCorpus, build_corpus


@pytest.fixture(scope="session")
def corpus() -> EvalCorpus:
    """One shared build: test_corpus_is_deterministic proves rebuilding changes nothing,
    so every other test can read the same instance instead of rebuilding per call."""

    return build_corpus()
