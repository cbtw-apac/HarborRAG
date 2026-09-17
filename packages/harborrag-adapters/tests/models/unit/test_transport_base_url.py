"""Unit tests for provider base URL policy."""

from __future__ import annotations

import pytest

from harborrag_adapters.models.runtime.transport import (
    CONTAINER_HOST_ALIASES,
    is_local_hostname,
    validate_base_url,
)

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
        "http://host.docker.internal:11434",
        "http://HOST.DOCKER.INTERNAL:11434",
        "http://gateway.docker.internal:11434",
        "http://host.lima.internal:11434",
        "http://host.containers.internal:11434",
    ],
)
def test_plain_http_is_allowed_for_loopback_and_container_host_aliases(url: str) -> None:
    validate_base_url(url, allowed_hosts=None, require_https=True)


def test_plain_http_to_a_remote_host_is_rejected_with_a_hint() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_base_url("http://models.example.com/v1", allowed_hosts=None, require_https=True)

    message = str(excinfo.value)
    assert message.startswith("remote provider base URLs must use HTTPS")
    assert "security.require_https_for_remote_endpoints" in message
    assert "security.allowed_base_url_hosts" in message


def test_plain_http_to_a_remote_host_is_allowed_when_policy_is_relaxed() -> None:
    validate_base_url(
        "http://models.example.com/v1",
        allowed_hosts=frozenset({"models.example.com"}),
        require_https=False,
    )


def test_container_host_alias_still_subject_to_allowlist() -> None:
    with pytest.raises(ValueError, match="is not allowed"):
        validate_base_url(
            "http://host.docker.internal:11434",
            allowed_hosts=frozenset({"localhost"}),
            require_https=True,
        )


def test_is_local_hostname_covers_every_alias() -> None:
    assert all(is_local_hostname(alias) for alias in CONTAINER_HOST_ALIASES)
    assert is_local_hostname("localhost")
    assert is_local_hostname("127.0.0.1")
    assert not is_local_hostname("models.example.com")
    assert not is_local_hostname("10.0.0.5")
