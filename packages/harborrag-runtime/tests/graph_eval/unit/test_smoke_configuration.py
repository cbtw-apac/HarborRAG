from __future__ import annotations

import pytest

from ..smoke.configuration import build_config

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    # Pin every variable build_config reads so a developer's env/.env.database
    # (loaded with override=False) cannot leak into the assertion.
    values = {
        "FALKORDB_HOST": "127.0.0.1",
        "FALKORDB_PORT": "6379",
        "FALKORDB_USERNAME": "",
        "FALKORDB_PASSWORD": "",
        "FALKORDB_SSL": "",
        "FALKORDB_ALLOW_INSECURE_REMOTE": "",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_local_plaintext_config_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, FALKORDB_PASSWORD="secret")
    config = build_config("harborrag-graph-eval")
    assert config.graph_name == "harborrag-graph-eval"
    assert config.ssl is False


def test_secure_remote_config_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, FALKORDB_HOST="graph.example.com", FALKORDB_SSL="true")
    assert build_config().ssl is True


def test_insecure_remote_config_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, FALKORDB_HOST="graph.example.com")
    with pytest.raises(ValueError, match="requires SSL"):
        build_config()
