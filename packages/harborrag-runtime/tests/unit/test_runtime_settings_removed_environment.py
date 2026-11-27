"""A removed storage-prefix variable is named in a WARNING, not silently dropped.

``HARBORRAG_QDRANT_COLLECTION_PREFIX`` kept two 2.0.0a1 deployments sharing
one Qdrant apart. It is no longer read, and settings ignore unknown variables,
so without a warning both sides would quietly land in the same namespace.
"""

from __future__ import annotations

import logging

import pytest

from harborrag_runtime.config import settings as settings_module
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit

_REMOVED_SETTING_EVENT = "config.removed_setting_ignored"


def _removed_setting_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the captured warnings about a removed variable."""
    return [
        record
        for record in caplog.records
        if record.__dict__.get("event") == _REMOVED_SETTING_EVENT
    ]


@pytest.fixture(autouse=True)
def _forget_earlier_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "_warned_removed_environment", set())


@pytest.mark.parametrize(
    "name",
    ["HARBORRAG_QDRANT_COLLECTION_PREFIX", "HARBORRAG_FALKORDB_TENANT_GRAPH_PREFIX"],
)
def test_a_removed_prefix_variable_warns_once_naming_its_replacement(
    name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(name, "staging_")

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.config"):
        RuntimeSettings()
        RuntimeSettings()

    warnings = [
        record for record in _removed_setting_warnings(caplog) if name in record.getMessage()
    ]
    assert len(warnings) == 1, "settings are built many times; the warning is not"
    assert warnings[0].levelno == logging.WARNING
    assert "HARBORRAG_STORAGE_NAMESPACE_PREFIX" in warnings[0].getMessage()
    assert "dropped_fields" not in warnings[0].__dict__


def test_no_warning_without_a_removed_variable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("HARBORRAG_QDRANT_COLLECTION_PREFIX", raising=False)
    monkeypatch.delenv("HARBORRAG_FALKORDB_TENANT_GRAPH_PREFIX", raising=False)

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.config"):
        RuntimeSettings()

    assert _removed_setting_warnings(caplog) == []
