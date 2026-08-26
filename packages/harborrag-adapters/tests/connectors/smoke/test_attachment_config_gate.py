"""Unit tests for the config-driven attachment pass gate in the smoke scripts.

`confluence.py`/`jira.py` skip their "load with attachments" pass entirely
when `include_attachments` is `false` in the connector's catalog settings,
and otherwise run it and propagate any attachment failure. These tests cover
both branches without touching real connectors, parsers, or the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import confluence
import jira
import pytest

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class _FakeConnector:
    """Stand-in for `HarborConnector` returning one canned record and document."""

    def __init__(self, document: RawDocument) -> None:
        self._document = document

    def discover(self, query):  # noqa: ARG002 - query is unused by the fake
        return [SourceRecord(id="fake://1", source_type="text/html", locator="1")]

    def load(self, record):  # noqa: ARG002 - record is unused by the fake
        return self._document


def _document(*, attachments: list[dict] | None = None) -> RawDocument:
    return RawDocument(
        id="fake://1",
        source="https://example.invalid/1",
        content="body",
        content_type="text/html",
        metadata={"attachments": attachments} if attachments is not None else {},
    )


def _definition(provider: str, *, include_attachments: bool) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"{provider}-main",
        provider=provider,
        settings={"include_attachments": include_attachments},
    )


@pytest.mark.parametrize("module,provider", [(confluence, "confluence"), (jira, "jira")])
def test_skips_attachment_pass_when_disabled_in_config(monkeypatch, module, provider) -> None:
    monkeypatch.setattr(module, "load_env", lambda: [])
    monkeypatch.setattr(
        module,
        "connector_definition",
        lambda identifier, *, expected_provider: _definition(
            expected_provider,
            include_attachments=False,
        ),
    )

    build_calls: list[bool] = []

    def fake_build_connector(  # noqa: ARG001
        name, *, include_attachments, parser=None, expected_provider=None
    ):
        build_calls.append(include_attachments)
        return _FakeConnector(_document())

    monkeypatch.setattr(module, "build_connector", fake_build_connector)
    monkeypatch.setattr(
        module,
        "build_harbor_parser",
        lambda: pytest.fail("attachment pass must not build a parser"),
    )

    run = module.run_confluence if provider == "confluence" else module.run_jira
    assert run(limit=1) == 0
    assert build_calls == [False]


@pytest.mark.parametrize("module,provider", [(confluence, "confluence"), (jira, "jira")])
def test_runs_attachment_pass_and_reports_success_when_enabled(
    monkeypatch, module, provider
) -> None:
    monkeypatch.setattr(module, "load_env", lambda: [])
    monkeypatch.setattr(
        module,
        "connector_definition",
        lambda identifier, *, expected_provider: _definition(
            expected_provider,
            include_attachments=True,
        ),
    )
    monkeypatch.setattr(module, "build_harbor_parser", lambda: object())

    without_attachments = _FakeConnector(_document())
    with_attachments = _FakeConnector(
        _document(attachments=[{"title": "a.txt", "status": "processed", "text": "hi"}])
    )
    build_calls: list[bool] = []

    def fake_build_connector(  # noqa: ARG001
        name, *, include_attachments, parser=None, expected_provider=None
    ):
        build_calls.append(include_attachments)
        return with_attachments if include_attachments else without_attachments

    monkeypatch.setattr(module, "build_connector", fake_build_connector)

    run = module.run_confluence if provider == "confluence" else module.run_jira
    assert run(limit=1) == 0
    assert build_calls == [False, True]


@pytest.mark.parametrize("module,provider", [(confluence, "confluence"), (jira, "jira")])
def test_propagates_attachment_failure_when_enabled(monkeypatch, module, provider) -> None:
    monkeypatch.setattr(module, "load_env", lambda: [])
    monkeypatch.setattr(
        module,
        "connector_definition",
        lambda identifier, *, expected_provider: _definition(
            expected_provider,
            include_attachments=True,
        ),
    )
    monkeypatch.setattr(module, "build_harbor_parser", lambda: object())

    without_attachments = _FakeConnector(_document())
    with_attachments = _FakeConnector(
        _document(attachments=[{"title": "a.pdf", "status": "failed", "reason": "boom"}])
    )

    def fake_build_connector(  # noqa: ARG001
        name, *, include_attachments, parser=None, expected_provider=None
    ):
        return with_attachments if include_attachments else without_attachments

    monkeypatch.setattr(module, "build_connector", fake_build_connector)

    run = module.run_confluence if provider == "confluence" else module.run_jira
    assert run(limit=1) == 1
