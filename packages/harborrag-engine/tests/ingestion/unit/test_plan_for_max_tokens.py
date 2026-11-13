"""`chunking.max_tokens` override wiring: `plan_for_max_tokens` caps one run's chunks.

Mirrors how `DocumentMaterializationStages.chunk_and_validate` builds a
`ChunkingPlan` from an override: only `hard_maximum_tokens` is driven by the
setting, the rest comes from the connector's own configured profile.
"""

from __future__ import annotations

from harborrag_core.domain.element import DocumentElement

from .chunking_helpers import make_document, make_profile, make_request, make_service


def test_plan_for_max_tokens_caps_the_hard_maximum_and_clamps_the_rest() -> None:
    profile = make_profile(name="jira", strategy="canonical", minimum=80, target=600, maximum=1000)
    service = make_service(profile)

    plan = service.plan_for_max_tokens("jira", 20)

    assert plan.profile == "jira"
    assert plan.hard_maximum_tokens == 20
    # Clamped from above, order preserved: minimum <= target <= soft <= hard.
    assert plan.minimum_tokens == 20
    assert plan.target_tokens == 20
    assert plan.soft_maximum_tokens == 20


def test_plan_for_max_tokens_leaves_limits_below_the_cap_untouched() -> None:
    profile = make_profile(name="jira", strategy="canonical", minimum=2, target=10, maximum=15)
    service = make_service(profile)

    plan = service.plan_for_max_tokens("jira", 256)

    assert plan.minimum_tokens == 2
    assert plan.target_tokens == 10
    assert plan.hard_maximum_tokens == 256


def test_a_capped_plan_actually_bounds_every_chunk_produced() -> None:
    profile = make_profile(name="canonical", strategy="canonical", minimum=2, target=20, maximum=25)
    service = make_service(profile)
    long_text = "word " * 100
    document = make_document([DocumentElement("body", "paragraph", long_text)])
    request = make_request(document)

    plan = service.plan_for_max_tokens("local", 8)
    result = service.chunk(request, plan)

    assert result.chunks
    assert all(chunk.token_count <= 8 for chunk in result.chunks)
