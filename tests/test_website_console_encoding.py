"""Tests for the website scripts' UTF-8 console guard."""

from __future__ import annotations

import sys

import pytest
from website.console_encoding import enable_utf8_output


class RecordingStream:
    """Stand-in for a text stream that records reconfigure calls."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.error = error

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class PlainStream:
    """Stand-in for a stream a harness replaced, which cannot be reconfigured."""


def test_reconfigures_both_streams_to_utf8(monkeypatch):
    out, err = RecordingStream(), RecordingStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    enable_utf8_output()

    expected = {"encoding": "utf-8", "errors": "replace"}
    assert out.calls == [expected]
    assert err.calls == [expected]


def test_skips_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", PlainStream())
    monkeypatch.setattr(sys, "stderr", PlainStream())

    enable_utf8_output()


@pytest.mark.parametrize("error", [ValueError("detached"), OSError("closed")])
def test_ignores_streams_that_refuse_reconfiguration(monkeypatch, error):
    stream = RecordingStream(error=error)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    enable_utf8_output()

    assert len(stream.calls) == 2
