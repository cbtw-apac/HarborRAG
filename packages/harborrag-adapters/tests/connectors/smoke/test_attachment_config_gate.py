"""Unit tests for the config-driven attachment pass gate in the smoke scripts.

`confluence.py`/`jira.py` skip their attachment pass entirely when
`include_attachments` is `false` in the connector's catalog settings, and
otherwise load and parse each attachment as its own document and propagate any
failure. These tests cover
both branches without touching real connectors, parsers, or the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import confluence
import jira
import pytest

from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class _FakeConnector:
    """Stand-in for `HarborConnector`: one record, bound to the given attachments."""

    def __init__(self, attachments: tuple[SourceRecord, ...] = ()) -> None:
        self._attachments = attachments

    def discover(self, query):  # noqa: ARG002 - query is unused by the fake
        return [SourceRecord(id="fake://1", source_type="text/html", locator="1")]

    def describe(self, record):  # noqa: ARG002 - record is unused by the fake
        return SimpleNamespace(bound_records=self._attachments)

    def load(self, record):
        return RawDocument(
            id=record.id,
            source="https://example.invalid/1",
            content="body",
            content_type="text/html",
        )


class _FakeParser:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def parse(self, raw):
        if self._fail:
            raise ValueError("boom")
        return ParsedDocument(content=f"parsed:{raw.id}", parser_name="fake")


_ATTACHMENT = SourceRecord(id="fake://1/attachments/a1", source_type="text/plain", locator="a1")


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
        name, *, include_attachments, expected_provider=None
    ):
        build_calls.append(include_attachments)
        return _FakeConnector()

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
    monkeypatch.setattr(module, "build_harbor_parser", _FakeParser)

    without_attachments = _FakeConnector()
    with_attachments = _FakeConnector((_ATTACHMENT,))
    build_calls: list[bool] = []

    def fake_build_connector(  # noqa: ARG001
        name, *, include_attachments, expected_provider=None
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
    monkeypatch.setattr(module, "build_harbor_parser", lambda: _FakeParser(fail=True))

    without_attachments = _FakeConnector()
    with_attachments = _FakeConnector((_ATTACHMENT,))

    def fake_build_connector(  # noqa: ARG001
        name, *, include_attachments, expected_provider=None
    ):
        return with_attachments if include_attachments else without_attachments

    monkeypatch.setattr(module, "build_connector", fake_build_connector)

    run = module.run_confluence if provider == "confluence" else module.run_jira
    assert run(limit=1) == 1
