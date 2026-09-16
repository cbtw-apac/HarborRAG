"""Prompt-catalog behavior for runtime chat."""

from __future__ import annotations

import pytest

from harborrag_runtime.chat import ChatPrompt, PromptCatalog


def test_prompt_catalog_resolves_named_templates() -> None:
    requested: list[str] = []
    catalog = PromptCatalog(lambda filename: requested.append(filename) or "  Prompt text.  ")

    assert catalog.resolve(ChatPrompt.CONCISE) == "Prompt text."
    assert requested == ["concise.md"]


def test_packaged_prompts_are_available_and_non_empty() -> None:
    catalog = PromptCatalog.packaged()

    assert catalog.resolve(ChatPrompt.DEFAULT).startswith("You are Harbor Assistance")
    assert "concisely" in catalog.resolve(ChatPrompt.CONCISE)
    gate = catalog.resolve(ChatPrompt.QUERY_GATE)
    assert "Harbor Assistance" in gate
    assert '{"scope":"unsupported"}' in gate
    assert "named-project" in gate and "repository" in gate


def test_prompt_catalog_rejects_empty_templates() -> None:
    catalog = PromptCatalog(lambda _filename: "  ")

    with pytest.raises(ValueError, match="is empty"):
        catalog.resolve(ChatPrompt.DEFAULT)
