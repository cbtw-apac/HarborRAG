"""PROJECT-scope memory extraction is opt-in (R3-04)."""

from __future__ import annotations

import os

import pytest

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory.policy import memory_policy_from_settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_ambient_harborrag_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide these cases from their arguments, never from the ambient environment."""

    for name in [key for key in os.environ if key.startswith("HARBORRAG_")]:
        monkeypatch.delenv(name, raising=False)


def test_project_scope_extraction_is_off_by_default() -> None:
    settings = RuntimeSettings()

    assert settings.memory_extraction_project_scope is False
    assert memory_policy_from_settings(settings).extraction_project_scope is False


def test_project_scope_extraction_is_switched_on_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_MEMORY_EXTRACTION_PROJECT_SCOPE", "true")

    assert memory_policy_from_settings(RuntimeSettings()).extraction_project_scope is True
