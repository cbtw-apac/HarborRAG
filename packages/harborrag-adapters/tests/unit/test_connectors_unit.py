"""White-box unit tests for shared connector infrastructure.

Covers the low-level, provider-agnostic helpers used by every connector:
``http_utils`` (retry/backoff math, capped streaming reads, error redaction,
origin checks), the shared ``AttachmentProcessor`` download/parse pipeline, and
the ``ConnectorRegistry``. Everything is exercised in isolation with tiny
in-memory fakes so no network, credentials, or model downloads are needed.
"""
from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from harbor_test_builders import FakeResponse

from harborrag_adapters.connectors.attachments import (
    AttachmentProcessor,
    FileType,
    classify_attachment,
)
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.http_utils import (
    ResponseTooLargeError,
    read_capped_content,
    require_same_origin_url,
    retry_delay_seconds,
    safe_error_detail,
    same_origin,
)
from harborrag_adapters.connectors.registry import ConnectorRegistry
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord


# ---------------------------------------------------------------------------
# http_utils.retry_delay_seconds
# ---------------------------------------------------------------------------


def test_retry_delay_clamps_to_max_delay():
    delay = retry_delay_seconds(
        {"Retry-After": "999999"},
        fallback_delay=1.0,
        max_delay=5.0,
        jitter=False,
    )
    assert delay == 5.0


def test_retry_delay_honors_numeric_retry_after_deterministically():
    delay = retry_delay_seconds(
        {"Retry-After": "10"},
        fallback_delay=1.0,
        jitter=False,
    )
    assert delay == 10.0


def test_retry_delay_honors_http_date_retry_after():
    # A date ~60s in the future should produce a positive, sub-max delay.
    future = time.strftime(
        "%a, %d %b %Y %H:%M:%S GMT",
        time.gmtime(time.time() + 60),
    )
    delay = retry_delay_seconds(
        {"Retry-After": future},
        fallback_delay=1.0,
        max_delay=300.0,
        jitter=False,
    )
    assert 0.0 < delay <= 300.0
    assert delay > 30.0  # comfortably away from "now"


def test_retry_delay_past_http_date_is_zero():
    past = time.strftime(
        "%a, %d %b %Y %H:%M:%S GMT",
        time.gmtime(time.time() - 3600),
    )
    delay = retry_delay_seconds(
        {"Retry-After": past},
        fallback_delay=1.0,
        jitter=False,
    )
    assert delay == 0.0


def test_retry_delay_uses_x_ratelimit_reset():
    reset_at = time.time() + 100
    delay = retry_delay_seconds(
        {"X-RateLimit-Reset": str(reset_at)},
        fallback_delay=1.0,
        max_delay=300.0,
        jitter=False,
    )
    # No exact equality: time advances between capture and call.
    assert 90.0 <= delay <= 100.0


def test_retry_delay_falls_back_without_headers():
    assert retry_delay_seconds(None, fallback_delay=2.5, jitter=False) == 2.5
    assert retry_delay_seconds({}, fallback_delay=2.5, jitter=False) == 2.5


def test_retry_delay_jitter_is_deterministic_when_disabled():
    kwargs = {"fallback_delay": 4.0, "jitter": False}
    first = retry_delay_seconds({"Retry-After": "4"}, **kwargs)
    second = retry_delay_seconds({"Retry-After": "4"}, **kwargs)
    assert first == second == 4.0


def test_retry_delay_jitter_stays_within_expected_band():
    # With jitter on, the delay is spread by at most 10% (capped at 1s).
    for _ in range(20):
        delay = retry_delay_seconds({"Retry-After": "10"}, fallback_delay=1.0)
        assert 10.0 <= delay <= 11.0


class _GetHeaderOnly:
    """Stub mimicking http.client.HTTPResponse: exposes getheader(), not get()."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def getheader(self, name: str) -> str | None:
        return self._values.get(name)


def test_retry_delay_reads_getheader_style_headers():
    headers = _GetHeaderOnly({"Retry-After": "7"})
    assert retry_delay_seconds(headers, fallback_delay=1.0, jitter=False) == 7.0


def test_retry_delay_getheader_missing_uses_fallback():
    headers = _GetHeaderOnly({})
    assert retry_delay_seconds(headers, fallback_delay=3.0, jitter=False) == 3.0


# ---------------------------------------------------------------------------
# http_utils.read_capped_content
# ---------------------------------------------------------------------------


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
    # A declared Content-Length over the cap is rejected up front (without
    # streaming the body). The pre-check must raise even though
    # ResponseTooLargeError subclasses ValueError.
    response = FakeResponse(
        headers={"Content-Length": "5000"},
        _chunks=[b"unused"],
    )
    with pytest.raises(ResponseTooLargeError, match="Content-Length"):
        read_capped_content(response, max_bytes=10)
    assert response.closed is True


def test_read_capped_content_incremental_cap_ignores_declared_content_length():
    # Even with a truthful-but-large Content-Length, protection comes from the
    # incremental cap on the streamed bytes, which does raise correctly.
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


# ---------------------------------------------------------------------------
# http_utils.safe_error_detail
# ---------------------------------------------------------------------------


def test_safe_error_detail_handles_none_and_empty():
    assert safe_error_detail(None) == ""
    assert safe_error_detail("") == ""


def test_safe_error_detail_truncates_long_bodies():
    detail = safe_error_detail("A" * 1000, limit=50)
    assert detail.endswith("… (truncated)")
    assert detail.startswith("A" * 50)


def test_safe_error_detail_collapses_newlines():
    assert safe_error_detail("line one\nline two") == "line one line two"


def test_safe_error_detail_redacts_secrets():
    detail = safe_error_detail("auth failed: password=supersecretvalue123")
    assert "supersecretvalue123" not in detail
    assert "<redacted>" in detail


# ---------------------------------------------------------------------------
# http_utils.require_same_origin_url / same_origin
# ---------------------------------------------------------------------------


def test_require_same_origin_allows_relative_urls():
    assert (
        require_same_origin_url("/path/x", "https://wiki.example.com", label="x")
        == "/path/x"
    )


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


# ---------------------------------------------------------------------------
# attachments.classify_attachment
# ---------------------------------------------------------------------------


def test_classify_prefers_filename_suffix_over_media_type():
    # A generic/misleading media type must not override a clear .csv suffix.
    assert classify_attachment("application/octet-stream", "data.csv") == (
        FileType.CSV,
        "csv",
    )


def test_classify_falls_back_to_media_type_map():
    assert classify_attachment("image/png", "screenshot") == (FileType.IMAGE, "png")


def test_classify_unknown_returns_none():
    assert classify_attachment("application/x-mystery", "thing.bin") is None


# ---------------------------------------------------------------------------
# attachments.AttachmentProcessor
# ---------------------------------------------------------------------------

BASE_URL = "https://wiki.example.com"


def _text_processor(**overrides) -> AttachmentProcessor:
    """Processor wired with a trivial text custom-parser (no HarborParser)."""
    defaults = dict(
        download_fn=lambda url: b"hello world",
        base_url=BASE_URL,
        custom_parsers={FileType.TEXT: lambda content, ext: content.decode()},
    )
    defaults.update(overrides)
    return AttachmentProcessor(**defaults)


def _attachment(**overrides) -> dict:
    data = {
        "id": "att-1",
        "title": "notes.txt",
        "mediaType": "text/plain",
        "size": 11,
        "downloadUrl": "/download/notes.txt",
    }
    data.update(overrides)
    return data


def test_attachment_processed_happy_path():
    processor = _text_processor()
    [result] = processor.process([_attachment()])

    assert result.status == "processed"
    assert result.text == "hello world"
    assert result.download_url == f"{BASE_URL}/download/notes.txt"
    assert result.id == "att-1"


def test_attachment_skipped_when_size_exceeds_max():
    downloaded: list[str] = []

    def download(url: str) -> bytes:
        downloaded.append(url)
        return b"x"

    processor = _text_processor(
        download_fn=download,
        max_attachment_size_bytes=5,
    )
    [result] = processor.process([_attachment(size=5000)])

    assert result.status == "skipped"
    assert "exceeds max_attachment_size_bytes" in result.reason
    assert downloaded == []  # short-circuited before download


def test_attachment_unsupported_media_type():
    processor = _text_processor()
    [result] = processor.process(
        [_attachment(title="mystery.bin", mediaType="application/x-mystery")]
    )

    assert result.status == "unsupported"
    assert "no handler" in result.reason


def test_attachment_failed_when_download_returns_none():
    processor = _text_processor(download_fn=lambda url: None)
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "download failed" in result.reason


def test_attachment_fail_on_error_reraises():
    def boom(content: bytes, ext: str) -> str:
        raise RuntimeError("parser exploded")

    processor = _text_processor(
        custom_parsers={FileType.TEXT: boom},
        fail_on_error=True,
    )
    with pytest.raises(RuntimeError, match="parser exploded"):
        processor.process([_attachment()])


def test_attachment_error_without_fail_on_error_marks_failed():
    def boom(content: bytes, ext: str) -> str:
        raise RuntimeError("parser exploded")

    processor = _text_processor(custom_parsers={FileType.TEXT: boom})
    [result] = processor.process([_attachment()])

    assert result.status == "failed"
    assert "parser exploded" in result.reason


def test_attachment_cross_origin_download_url_rejected():
    processor = _text_processor()
    [result] = processor.process(
        [_attachment(downloadUrl="https://evil.example.com/steal")]
    )

    assert result.status == "skipped"
    assert "outside trusted origin" in result.reason


def test_attachment_callback_can_skip():
    processor = _text_processor(
        process_attachment_callback=lambda media, size, title: (False, "policy denied"),
    )
    [result] = processor.process([_attachment()])

    assert result.status == "skipped"
    assert result.reason == "policy denied"


def test_attachment_downloaded_body_over_cap_is_skipped():
    # Pre-download size is fine, but the actual body is larger than the cap.
    processor = _text_processor(
        download_fn=lambda url: b"x" * 100,
        max_attachment_size_bytes=10,
    )
    [result] = processor.process([_attachment(size=1)])

    assert result.status == "skipped"
    assert "downloaded size" in result.reason


# ---------------------------------------------------------------------------
# registry.ConnectorRegistry
# ---------------------------------------------------------------------------


class _DummyConnector(BaseConnector):
    provider_name = "dummy"

    def discover(self, query=None) -> Iterator[SourceRecord]:
        yield from ()

    def load(self, record: SourceRecord) -> RawDocument:  # pragma: no cover - unused
        raise NotImplementedError


class _OtherConnector(_DummyConnector):
    provider_name = "other"


def test_registry_registers_name_and_aliases():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector, aliases=["dm", "test-dummy"])

    assert registry.get_class("dummy") is _DummyConnector
    assert registry.get_class("dm") is _DummyConnector
    assert registry.get_class("test-dummy") is _DummyConnector


def test_registry_create_instantiates():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    assert isinstance(registry.create("dummy"), _DummyConnector)


def test_registry_duplicate_key_different_class_raises():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("dummy", _OtherConnector)


def test_registry_same_class_reregister_is_noop():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    registry.register("dummy", _DummyConnector)  # idempotent, no error
    assert registry.get_class("dummy") is _DummyConnector


def test_registry_replace_allows_override():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    registry.register("dummy", _OtherConnector, replace=True)
    assert registry.get_class("dummy") is _OtherConnector


def test_registry_names_are_sorted():
    registry = ConnectorRegistry()
    registry.register("zeta", _DummyConnector)
    registry.register("alpha", _OtherConnector, aliases=["mid"])
    assert registry.names() == ["alpha", "mid", "zeta"]


def test_registry_unknown_name_raises():
    from harborrag_adapters.connectors.exceptions import ConnectorNotFoundError

    registry = ConnectorRegistry()
    with pytest.raises(ConnectorNotFoundError):
        registry.get_class("nope")
