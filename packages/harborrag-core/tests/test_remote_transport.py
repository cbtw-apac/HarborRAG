from __future__ import annotations

import pytest

from harborrag_core.security import RemoteTransportPolicy, is_loopback_host

_POLICY = RemoteTransportPolicy(
    service="cache",
    allowed_schemes=frozenset({"cache", "caches"}),
    secure_schemes=frozenset({"caches"}),
)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "127.0.0.1", "[::1]"])
def test_plaintext_transport_is_allowed_only_for_explicit_loopback(host: str) -> None:
    parsed = _POLICY.validate(f"cache://{host}:1234")

    assert parsed.hostname is not None
    assert is_loopback_host(parsed.hostname)


def test_remote_transport_requires_encryption_or_explicit_acknowledgement() -> None:
    with pytest.raises(ValueError, match="requires an encrypted transport"):
        _POLICY.validate("cache://cache.internal:1234")

    _POLICY.validate("caches://cache.internal:1234")
    _POLICY.validate(
        "cache://user:secret@cache.internal:1234",
        allow_insecure_remote=True,
    )


@pytest.mark.parametrize(
    "url",
    ["cache:///missing-host", "https://cache.internal", "cache://host:not-a-port"],
)
def test_remote_transport_rejects_malformed_or_unexpected_urls(url: str) -> None:
    with pytest.raises(ValueError):
        _POLICY.validate(url)
