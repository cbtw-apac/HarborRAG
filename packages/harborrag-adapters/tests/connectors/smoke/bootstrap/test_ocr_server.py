"""Unit tests for host-reachable OCR server resolution in the smoke bootstrap."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bootstrap import ocr_server
from harborrag_adapters.parsers.pdf.engines.liteparse.config import LiteParsePDFConfig

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

CONTAINER_URL = "http://ppocr-server:8888/ocr"


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ocr_server.OCR_SERVER_URL_VARIABLE, raising=False)


def _unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr_server, "_resolvable", lambda hostname: False)


def _resolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr_server, "_resolvable", lambda hostname: True)


def test_unresolvable_container_host_falls_back_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unresolvable(monkeypatch)

    assert ocr_server.host_ocr_server_url(CONTAINER_URL) == "http://localhost:8888/ocr"


def test_resolvable_host_is_used_exactly_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolvable(monkeypatch)

    assert ocr_server.host_ocr_server_url(CONTAINER_URL) == CONTAINER_URL


def test_loopback_url_needs_no_name_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(hostname: str) -> bool:
        raise AssertionError(f"resolution attempted for {hostname!r}")

    monkeypatch.setattr(ocr_server, "_resolvable", fail)

    assert ocr_server.host_ocr_server_url("http://localhost:8888/ocr") == (
        "http://localhost:8888/ocr"
    )


def test_credentials_and_default_port_survive_the_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unresolvable(monkeypatch)

    assert ocr_server.host_ocr_server_url("https://ocr:secret@ppocr-server/v2/ocr") == (
        "https://ocr:secret@localhost/v2/ocr"
    )


def test_explicit_override_wins_without_touching_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(hostname: str) -> bool:
        raise AssertionError(f"resolution attempted for {hostname!r}")

    monkeypatch.setattr(ocr_server, "_resolvable", fail)
    monkeypatch.setenv(ocr_server.OCR_SERVER_URL_VARIABLE, "http://ocr.internal:9000/ocr")

    assert ocr_server.host_ocr_server_url(CONTAINER_URL) == "http://ocr.internal:9000/ocr"


def test_engines_without_a_configured_ocr_server_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unresolvable(monkeypatch)
    options = LiteParsePDFConfig(ocr_server_url=None)
    harbor_parser = _registry(_engine("liteparse", options))

    ocr_server.apply_host_ocr_server_url(harbor_parser)

    assert options.ocr_server_url is None


def test_apply_rewrites_every_configured_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    _unresolvable(monkeypatch)
    liteparse_options = LiteParsePDFConfig(ocr_server_url=CONTAINER_URL)
    harbor_parser = _registry(
        _engine("liteparse", liteparse_options),
        _engine("pymupdf", SimpleNamespace()),
    )

    ocr_server.apply_host_ocr_server_url(harbor_parser)

    assert liteparse_options.ocr_server_url == "http://localhost:8888/ocr"


def _engine(name: str, options: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, options=options)


def _registry(*engines: SimpleNamespace) -> SimpleNamespace:
    """Stand in for `HarborParserRegistry` with one PDF-shaped family."""
    pdf_family = SimpleNamespace(parser_name="pdf", engines=tuple(engines))
    text_family = SimpleNamespace(parser_name="text")
    return SimpleNamespace(families=lambda: (pdf_family, text_family))
