"""Credential leakage and error-detail redaction tests."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from harborrag_adapters.connectors.utils.http import safe_error_detail
from harborrag_core.domain.raw_document import RawDocument

pytestmark = pytest.mark.blackbox


def test_connector_config_reprs_redact_secrets() -> None:
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.jira.config import JiraProjectConfig
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig

    gh = GitHubRepositoryConfig(owner="o", repo="r", token="ghp_SECRET")
    jira = JiraProjectConfig(base_url="https://x.atlassian.net", email="a@b.c", token="jira_SECRET")
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


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ("api_key=api-secret-123", "api-secret-123"),
        ("password: password-secret-123", "password-secret-123"),
        ("secret=service-secret-123", "service-secret-123"),
        ("Authorization: Bearer bearer-secret-123", "bearer-secret-123"),
        (
            "GitHub ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        ),
        ("AWS AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        ("OpenAI sk-ABCDEFGHIJKLMNOPQRST", "sk-ABCDEFGHIJKLMNOPQRST"),
        (
            "Google AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        ),
        (repr({"token": "mapping-secret-123"}), "mapping-secret-123"),
    ],
)
def test_safe_error_detail_redacts_required_secret_patterns(payload: str, secret: str) -> None:
    assert secret not in safe_error_detail(payload)


def test_safe_error_detail_truncates() -> None:
    detail = safe_error_detail("A" * 2000)
    assert len(detail) <= 600
    assert safe_error_detail(None) == ""


def _smoke_bootstrap() -> dict[str, object]:
    path = Path(__file__).parents[1] / "smoke" / "bootstrap.py"
    return runpy.run_path(str(path))


def test_smoke_document_output_hides_provider_content_by_default(monkeypatch, capsys) -> None:
    monkeypatch.delenv("HARBOR_SMOKE_VERBOSE", raising=False)
    document = RawDocument(
        id="safe-id",
        source="https://provider.example/private",
        content="confidential document body",
        content_type="text/plain",
        metadata={"token": "metadata-secret", "title": "private title"},
        raw={"secret": "raw-secret"},
    )

    _smoke_bootstrap()["print_document"]("provider", document)
    output = capsys.readouterr().out

    assert "safe-id" in output
    assert "confidential document body" not in output
    assert "private title" not in output
    assert "metadata-secret" not in output
    assert "raw-secret" not in output
    assert "provider.example/private" not in output


def test_verbose_smoke_previews_are_bounded_and_redacted(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("HARBOR_SMOKE_VERBOSE", "1")
    secret = "verbose-secret-123"
    document = RawDocument(
        id="safe-id",
        source=f"https://provider.example/?token={secret}",
        content=f"token={secret} " + "A" * 2_000,
        content_type="text/plain",
        metadata={"api_key": secret},
        raw={"password": secret},
    )

    _smoke_bootstrap()["print_document"]("provider", document)
    output = capsys.readouterr().out

    assert secret not in output
    assert "truncated" in output


def test_ci_disables_smoke_previews_even_when_requested(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("HARBOR_SMOKE_VERBOSE", "1")
    document = RawDocument(
        id="safe-id",
        source="https://provider.example/private",
        content="confidential-ci-content",
        content_type="text/plain",
    )

    _smoke_bootstrap()["print_document"]("provider", document)
    output = capsys.readouterr().out

    assert "confidential-ci-content" not in output
    assert "provider.example/private" not in output
