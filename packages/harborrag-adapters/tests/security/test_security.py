"""Security tests: unsafe paths, SSRF, malicious files, and credential leakage.

Each test pins a specific hardening fix so a regression fails loudly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.connectors.http_utils import (
    require_same_origin_url,
    safe_error_detail,
)
from harborrag_adapters.parsers import HarborParser
from harborrag_adapters.parsers.exceptions import ParseError
from harborrag_adapters.parsers.pdf_engine.utils import materialized_pdf_path
from harborrag_adapters.parsers.utils import open_guarded_zip
from harborrag_core.domain.parser import ParseInput

from harbor_test_builders import build_zip_bomb_bytes


# --------------------------------------------------------------------------- #
# C1 — PDF temp-file materialization must never honor an untrusted filename.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "malicious_name",
    [
        "../../etc/cron.d/evil.pdf",
        "/etc/cron.d/evil.pdf",
        "..\\..\\windows\\system32\\evil.pdf",
        "....//....//evil.pdf",
    ],
)
def test_materialized_pdf_path_ignores_untrusted_filename(malicious_name: str) -> None:
    parse_input = ParseInput(content=b"%PDF-1.4 fake", filename=malicious_name)
    with materialized_pdf_path(parse_input) as path:
        # Always a fixed basename inside the temp dir — never the attacker path.
        assert path.name == "document.pdf"
        assert path.parent.name.startswith("harborrag-pdf-")
        assert not str(path).endswith("cron.d/evil.pdf")
        assert path.read_bytes() == b"%PDF-1.4 fake"


# --------------------------------------------------------------------------- #
# C2 — bare strings are content, not filesystem reads (no arbitrary file read).
# --------------------------------------------------------------------------- #
def test_string_input_is_never_read_as_a_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    parse_input = ParseInput.coerce(str(secret))
    # Coerced as content: the string itself, not the file's contents.
    assert parse_input.content == str(secret)
    assert parse_input.path is None
    assert "TOP SECRET" not in parse_input.read_text()


def test_path_strings_only_read_when_explicitly_opted_in(tmp_path: Path) -> None:
    target = tmp_path / "doc.txt"
    target.write_text("real body", encoding="utf-8")
    parse_input = ParseInput.coerce(str(target), allow_path_strings=True)
    assert parse_input.path == target


# --------------------------------------------------------------------------- #
# C3 — same-origin guard cannot be bypassed by scheme casing / odd schemes.
# --------------------------------------------------------------------------- #
def test_require_same_origin_allows_relative_and_same_origin() -> None:
    base = "https://example.atlassian.net/wiki"
    assert require_same_origin_url("/download/x", base, label="t") == "/download/x"
    assert (
        require_same_origin_url("https://example.atlassian.net/a", base, label="t")
        == "https://example.atlassian.net/a"
    )


@pytest.mark.parametrize(
    "hostile",
    [
        "HTTPS://evil.example.com/x",  # uppercase scheme (the original bypass)
        "https://evil.example.com/x",  # cross origin
        "http://example.atlassian.net/x",  # scheme mismatch (http vs https)
        "file:///etc/passwd",  # non-http scheme
        "gopher://evil/x",
    ],
)
def test_require_same_origin_rejects_cross_origin_and_odd_schemes(hostile: str) -> None:
    with pytest.raises(ValueError):
        require_same_origin_url(
            hostile, "https://example.atlassian.net/wiki", label="attachment"
        )


# --------------------------------------------------------------------------- #
# H2 — decompression bombs are rejected before members are read.
# --------------------------------------------------------------------------- #
def test_open_guarded_zip_rejects_compression_bomb() -> None:
    with pytest.raises(ParseError, match="ratio|uncompressed|members"):
        open_guarded_zip(build_zip_bomb_bytes())


def test_epub_parser_rejects_bomb_via_public_api() -> None:
    parser = HarborParser()
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=build_zip_bomb_bytes(), filename="b.epub"))


# --------------------------------------------------------------------------- #
# XXE — EPUB XML is parsed with defusedxml when available.
# --------------------------------------------------------------------------- #
def test_epub_xml_uses_defused_parser() -> None:
    from harborrag_adapters.parsers import ebook

    # The billion-laughs / external-entity mitigations come from defusedxml;
    # confirm the parser bound the hardened implementation, not stdlib.
    assert "defusedxml" in ebook._xml_fromstring.__module__


# --------------------------------------------------------------------------- #
# Credential leakage — secrets never appear in repr; bodies are redacted.
# --------------------------------------------------------------------------- #
def test_connector_config_reprs_redact_secrets() -> None:
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.jira.config import JiraProjectConfig
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig

    gh = GitHubRepositoryConfig(owner="o", repo="r", token="ghp_SECRET")
    jira = JiraProjectConfig(
        base_url="https://x.atlassian.net", email="a@b.c", token="jira_SECRET"
    )
    sp = SharePointSiteConfig(
        site_url="https://x.sharepoint.com/sites/s",
        access_token="graph_SECRET",
        client_secret="client_SECRET",
    )
    for text, secret in [
        (repr(gh), "ghp_SECRET"),
        (repr(jira), "jira_SECRET"),
        (repr(sp), "graph_SECRET"),
        (repr(sp), "client_SECRET"),
    ]:
        assert secret not in text


def test_safe_error_detail_truncates_and_redacts() -> None:
    detail = safe_error_detail("token=abc123 " + "A" * 2000)
    assert "abc123" not in detail
    assert len(detail) <= 600
    assert safe_error_detail(None) == ""
