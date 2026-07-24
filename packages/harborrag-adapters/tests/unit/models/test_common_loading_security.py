from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from harborrag_adapters.models.common.environment import expand_environment
from harborrag_adapters.models.common.loading import (
    load_config_document,
    prepare_config_section,
)
from harborrag_adapters.models.common.security import (
    PrivacyConfig,
    PrivacySanitizer,
    SecretReference,
    resolve_secret_references,
    reveal_secret,
)
from harborrag_adapters.models.common.transport import (
    protect_sensitive_headers,
    reveal_headers,
    validate_base_url,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class SecretResolver:
    def resolve(self, reference: SecretReference) -> str:
        return f"resolved:{reference.uri}"


def test_common_config_loading_environment_and_secrets(tmp_path: Path, monkeypatch) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("embed:\n  default_model: primary\n", encoding="utf-8")
    json_path = tmp_path / "config.json"
    json_path.write_text(json.dumps({"rerank": {"default_model": "primary"}}), encoding="utf-8")
    assert "embed" in load_config_document(yaml_path)
    assert "rerank" in load_config_document(json_path)
    bad_path = tmp_path / "config.txt"
    bad_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="yaml"):
        load_config_document(bad_path)
    list_path = tmp_path / "list.yaml"
    list_path.write_text("- a\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config_document(list_path)

    document = {
        "embed": {"timeout": 1, "nested": {"a": 1}},
        "profiles": {"prod": {"embed": {"timeout": 2, "nested": {"b": 2}}}},
    }
    prepared = prepare_config_section(
        document, section="embed", profile="prod", overrides={"nested": {"c": 3}}
    )
    assert prepared == {"timeout": 2, "nested": {"a": 1, "b": 2, "c": 3}}
    with pytest.raises(ValueError, match="unknown"):
        prepare_config_section(document, section="embed", profile="missing", overrides=None)
    with pytest.raises(ValueError, match="must be a mapping"):
        prepare_config_section({"embed": []}, section="embed", profile=None, overrides=None)
    with pytest.raises(ValueError, match="must be a mapping"):
        prepare_config_section(
            {"embed": {}, "profiles": {"bad": {"embed": []}}},
            section="embed",
            profile="bad",
            overrides=None,
        )

    monkeypatch.setenv("API_KEY", "secret-value")
    monkeypatch.setenv("SECRET_URI", "secret://vault/key")
    assert expand_environment("${API_KEY}") == "secret-value"
    assert expand_environment("prefix-${API_KEY}") == "prefix-secret-value"
    assert expand_environment("${MISSING:-fallback}") == "fallback"
    assert expand_environment("${SECRET_URI}") == "secret://vault/key"
    assert expand_environment("secret://vault/direct") == "secret://vault/direct"
    assert resolve_secret_references(expand_environment("${SECRET_URI}"), None) == {
        "uri": "secret://vault/key"
    }
    assert expand_environment({"x": ["${API_KEY}", ("${API_KEY}",)]}) == {
        "x": ["secret-value", ("secret-value",)]
    }
    with pytest.raises(ValueError, match="not set"):
        expand_environment("${ABSENT}")
    with pytest.raises(ValueError, match="not set"):
        expand_environment("prefix-${ABSENT}")

    resolver = SecretResolver()
    reference = SecretReference(uri="secret://vault/key")
    assert str(reference) == "**********"
    assert "**********" in repr(reference)
    assert resolve_secret_references(reference, resolver).get_secret_value().startswith("resolved:")
    nested = resolve_secret_references(
        {"a": {"uri": "secret://vault/a"}, "b": [reference], "c": (reference,)},
        resolver,
    )
    assert nested["a"].get_secret_value().endswith("/a")
    assert resolve_secret_references(reference, None) is reference
    with pytest.raises(ValueError, match="unresolved"):
        reveal_secret(reference)
    assert reveal_secret(SecretStr("x")) == "x"
    assert reveal_secret(None) is None


def test_common_security_and_privacy_paths() -> None:
    assert protect_sensitive_headers("not-a-map") == "not-a-map"
    protected = protect_sensitive_headers({"Authorization": "Bearer x", "X": "y"})
    assert isinstance(protected["Authorization"], SecretStr)
    with pytest.raises(ValueError, match="invalid"):
        validate_base_url("ftp://example.com", allowed_hosts=None, require_https=True)
    with pytest.raises(ValueError, match="HTTPS"):
        validate_base_url("http://example.com", allowed_hosts=None, require_https=True)
    with pytest.raises(ValueError, match="not allowed"):
        validate_base_url(
            "https://example.com",
            allowed_hosts=frozenset({"allowed.example.com"}),
            require_https=True,
        )
    validate_base_url("http://localhost:11434", allowed_hosts=None, require_https=True)
    validate_base_url(None, allowed_hosts=None, require_https=True)

    privacy = PrivacyConfig(
        log_inputs=True,
        log_outputs=True,
        metadata_allowlist=frozenset({"user_id", "tenant_id", "workflow_id"}),
        max_logged_content_length=4,
    )
    sanitizer = PrivacySanitizer(privacy)
    sanitized = sanitizer.sanitize(
        {"api_key": "secret", "value": "abcdefgh", "secret_object": SecretStr("x")}
    )
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["value"] == "abcd"
    assert sanitized["secret_object"] == "**********"
    metadata = sanitizer.metadata({"user_id": "u", "tenant_id": "t", "workflow_id": "abcdef"})
    assert metadata["user_id"] != "u"
    assert metadata["workflow_id"] == "abcd"
    assert reveal_headers({"X": "value", "Authorization": SecretStr("token")}) == {
        "X": "value",
        "Authorization": "token",
    }
