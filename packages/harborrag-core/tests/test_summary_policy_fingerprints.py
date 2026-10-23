"""Which policy edits invalidate which summary cards."""

from harborrag_core.summaries import CardWordBudgets, SummaryPolicy


def test_an_entity_word_budget_edit_keeps_every_leaf_card_cached() -> None:
    plain = SummaryPolicy(model_fingerprint="m")
    widened = plain.model_copy(update={"card_words": CardWordBudgets(source_entity=200)})
    # Bindings rebind on any model-affecting edit ...
    assert widened.fingerprint != plain.fingerprint
    # ... but section and document cards come back from cache: their generation
    # keys carry their own kind's budget, which did not change.
    assert widened.leaf_fingerprint == plain.leaf_fingerprint
    assert widened.card_words.for_kind("Structure") == plain.card_words.for_kind("Structure")


def test_the_leaf_fingerprint_still_tracks_the_model() -> None:
    assert (
        SummaryPolicy(model_fingerprint="m").leaf_fingerprint
        != SummaryPolicy(model_fingerprint="other").leaf_fingerprint
    )
