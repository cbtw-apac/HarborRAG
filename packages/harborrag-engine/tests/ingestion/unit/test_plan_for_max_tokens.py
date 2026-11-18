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
    request = make_request(make_document([], source="jira"))

    plan = service.plan_for_max_tokens(request, 20)

    assert plan.profile == "jira"
    assert plan.hard_maximum_tokens == 20
    # Clamped from above, order preserved: minimum <= target <= soft <= hard.
    assert plan.minimum_tokens == 20
    assert plan.target_tokens == 20
    assert plan.soft_maximum_tokens == 20


def test_plan_for_max_tokens_leaves_limits_below_the_cap_untouched() -> None:
    profile = make_profile(name="jira", strategy="canonical", minimum=2, target=10, maximum=15)
    service = make_service(profile)
    request = make_request(make_document([], source="jira"))

    plan = service.plan_for_max_tokens(request, 256)

    assert plan.minimum_tokens == 2
    assert plan.target_tokens == 10
    assert plan.hard_maximum_tokens == 256


def test_a_capped_plan_actually_bounds_every_chunk_produced() -> None:
    profile = make_profile(name="canonical", strategy="canonical", minimum=2, target=20, maximum=25)
    service = make_service(profile)
    long_text = "word " * 100
    document = make_document([DocumentElement("body", "paragraph", long_text)])
    request = make_request(document)

    plan = service.plan_for_max_tokens(request, 8)
    result = service.chunk(request, plan)

    assert result.chunks
    assert all(chunk.token_count <= 8 for chunk in result.chunks)


def test_plan_for_max_tokens_resolves_the_attachment_profile_override() -> None:
    """An ATTACHMENT binding plans against the source-neutral default profile.

    `run()` resolves the default profile for an independently parsed
    attachment via `_profile_override`, regardless of the connector its
    container came from; a `chunking.max_tokens` plan must follow the same
    rule instead of always reading the connector's own profile.
    """
    connector_profile = make_profile(
        name="jira", strategy="canonical", minimum=80, target=600, maximum=1000
    )
    default_profile = make_profile(
        name="canonical", strategy="canonical", minimum=2, target=20, maximum=25
    )
    service = make_service(
        default_profile,
        extra_profiles={connector_profile.name: connector_profile},
        source_profiles={"jira": connector_profile.name},
    )
    document = make_document(
        [DocumentElement("body", "paragraph", "word")],
        source="jira",
        extra={"binding_kind": "ATTACHMENT"},
    )
    request = make_request(document)

    plan = service.plan_for_max_tokens(request, 8)

    assert plan.profile == default_profile.name
