"""White-box unit tests for shared connector HTTP utilities."""

from __future__ import annotations

import pytest
from harbor_test_builders import FakeResponse

from harborrag_adapters.connectors.utils.http import (
    ResponseTooLargeError,
    read_capped_content,
    require_same_origin_url,
    safe_error_detail,
    same_origin,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_read_capped_content_joins_chunks_under_cap():
    response = FakeResponse(_chunks=[b"ab", b"cd", b"ef"])
    assert read_capped_content(response, max_bytes=1000) == b"abcdef"
    assert response.closed is False


def test_read_capped_content_no_cap_returns_all():
    response = FakeResponse(_chunks=[b"x" * 100])
    assert read_capped_content(response, max_bytes=None) == b"x" * 100


def test_read_capped_content_raises_when_stream_exceeds_cap():
    response = FakeResponse(_chunks=[b"12345", b"67890"])
    with pytest.raises(ResponseTooLargeError):
        read_capped_content(response, max_bytes=6)
    assert response.closed is True


def test_read_capped_content_content_length_precheck_rejects_before_reading():
    response = FakeResponse(
        headers={"Content-Length": "5000"},
        _chunks=[b"unused"],
    )
    with pytest.raises(ResponseTooLargeError, match="Content-Length"):
        read_capped_content(response, max_bytes=10)
    assert response.closed is True


def test_read_capped_content_incremental_cap_ignores_declared_content_length():
    response = FakeResponse(
        headers={"Content-Length": "5000"},
        _chunks=[b"x" * 50],
    )
    with pytest.raises(ResponseTooLargeError, match="exceeds cap"):
        read_capped_content(response, max_bytes=10)
    assert response.closed is True


def test_read_capped_content_ignores_unparseable_content_length():
    response = FakeResponse(
        headers={"Content-Length": "not-a-number"},
        _chunks=[b"hello"],
    )
    assert read_capped_content(response, max_bytes=10) == b"hello"


def test_safe_error_detail_handles_none_and_empty():
    assert safe_error_detail(None) == ""
    assert safe_error_detail("") == ""


def test_safe_error_detail_truncates_long_bodies():
    detail = safe_error_detail("A" * 1000, limit=50)
    assert detail.startswith("A" * 50)
    assert "(truncated)" in detail


def test_safe_error_detail_collapses_newlines():
    assert safe_error_detail("line one\nline two") == "line one line two"


def test_safe_error_detail_redacts_secrets():
    detail = safe_error_detail("auth failed: password=supersecretvalue123")
    assert "supersecretvalue123" not in detail
    assert "<redacted>" in detail


def test_require_same_origin_allows_relative_urls():
    assert require_same_origin_url("/path/x", "https://wiki.example.com", label="x") == "/path/x"


def test_require_same_origin_allows_same_origin_absolute():
    url = "https://wiki.example.com/download/1"
    assert require_same_origin_url(url, "https://wiki.example.com", label="x") == url


def test_require_same_origin_rejects_cross_origin():
    with pytest.raises(ValueError, match="outside trusted origin"):
        require_same_origin_url(
            "https://evil.example.com/x",
            "https://wiki.example.com",
            label="attachment download",
        )


def test_require_same_origin_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="scheme"):
        require_same_origin_url(
            "ftp://wiki.example.com/x",
            "https://wiki.example.com",
            label="attachment download",
        )


def test_same_origin_default_ports():
    assert same_origin("http://h/a", "http://h:80/b") is True
    assert same_origin("https://h/a", "https://h:443/b") is True
    assert same_origin("https://h:8443/a", "https://h/b") is False
    assert same_origin("http://h/a", "https://h/b") is False
    assert same_origin("http://one/a", "http://two/b") is False


def test_same_origin_scheme_without_known_default_port():
    assert same_origin("ftp://h/a", "ftp://h/b") is True


def test_read_capped_content_skips_empty_chunks():
    response = FakeResponse(_chunks=[b"", b"hello", b""])
    assert read_capped_content(response, max_bytes=None) == b"hello"
