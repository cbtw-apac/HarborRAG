"""Same-origin URL guard tests for attachment downloads."""

from __future__ import annotations

import pytest
from harborrag_adapters.connectors.utils.http import require_same_origin_url

pytestmark = pytest.mark.blackbox


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
        "HTTPS://evil.example.com/x",
        "https://evil.example.com/x",
        "http://example.atlassian.net/x",
        "file:///etc/passwd",
        "gopher://evil/x",
    ],
)
def test_require_same_origin_rejects_cross_origin_and_odd_schemes(hostile: str) -> None:
    with pytest.raises(ValueError, match="Unsafe attachment URL"):
        require_same_origin_url(
            hostile, "https://example.atlassian.net/wiki", label="attachment"
        )
