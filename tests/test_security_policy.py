"""The security policy and README describe the supported 2.0 release honestly."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_security_policy_has_a_supported_versions_table() -> None:
    assert "## Supported versions" in SECURITY
    assert re.search(r"^\| 2\.0\.x \| Yes\b", SECURITY, re.MULTILINE)
    assert re.search(r"^\| < 2\.0 .*\| No\b", SECURITY, re.MULTILINE)
    assert "Alpha" not in SECURITY
    assert "best-effort" not in SECURITY


def test_security_policy_states_the_threat_model_and_deployment_scope() -> None:
    section = SECURITY.split("## Threat model and deployment scope", 1)[1]

    for phrase in (
        "single-organisation",
        "HMAC",
        "OIDC",
        "Tenant isolation is enforced",
        "network perimeter",
        "HARBORRAG_ENV=prod",
    ):
        assert phrase in section, phrase


def test_security_policy_names_shipped_minio_and_mcp_controls_and_the_temporal_ui_limit() -> None:
    assert "least-privilege MinIO application key" in SECURITY
    assert "HARBORRAG_MCP_AUTH_MODE" in SECURITY
    assert "Temporal Web UI" in SECURITY.split("Known limits:", 1)[1]


def test_readme_release_note_names_no_pre_release() -> None:
    note = re.search(r"^> \*\*Release Note\*\*.*?(?=^[^>]|\Z)", README, re.MULTILINE | re.DOTALL)

    assert note is not None
    assert not re.search(r"\d+\.\d+\.\d+(?:a|b|rc)\d+", note.group(0))
    assert "SECURITY.md" in note.group(0)
