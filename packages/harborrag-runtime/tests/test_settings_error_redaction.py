"""Settings validation errors must not echo the rejected input."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_runtime.config.settings import RuntimeSettings

SYNTHETIC_SECRET = "synthetic-secret-value-0123"


def test_validation_error_does_not_echo_the_rejected_value() -> None:
    with pytest.raises(ValidationError) as caught:
        RuntimeSettings(env=SYNTHETIC_SECRET)  # type: ignore[arg-type]

    assert "env" in str(caught.value)
    assert SYNTHETIC_SECRET not in str(caught.value)


def test_environment_sourced_value_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_ENV", SYNTHETIC_SECRET)

    with pytest.raises(ValidationError) as caught:
        RuntimeSettings()

    assert SYNTHETIC_SECRET not in str(caught.value)
