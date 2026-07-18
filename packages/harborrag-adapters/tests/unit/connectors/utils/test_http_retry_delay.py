"""White-box unit tests for retry_delay_seconds header/backoff resolution."""

from __future__ import annotations

import time

import pytest
from harborrag_adapters.connectors.utils.http import retry_delay_seconds

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


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
    assert delay > 30.0


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
    for _ in range(20):
        delay = retry_delay_seconds({"Retry-After": "10"}, fallback_delay=1.0)
        assert 10.0 <= delay <= 11.0


def test_retry_delay_jitter_never_exceeds_max_delay():
    for _ in range(20):
        delay = retry_delay_seconds(
            {"Retry-After": "10"},
            fallback_delay=1.0,
            max_delay=10.0,
            jitter=True,
        )
        assert delay <= 10.0


class _GetHeaderOnly:
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


def test_retry_delay_falls_back_to_rate_limit_reset_header():
    reset_at = time.time() + 10
    delay = retry_delay_seconds(
        {"X-RateLimit-Reset": str(reset_at)},
        fallback_delay=1.0,
        jitter=False,
    )
    assert delay == pytest.approx(10.0, abs=1.0)


def test_retry_delay_ignores_unparseable_rate_limit_reset():
    delay = retry_delay_seconds(
        {"X-RateLimit-Reset": "not-a-number"},
        fallback_delay=2.5,
        jitter=False,
    )
    assert delay == 2.5


def test_retry_delay_uses_fallback_when_headers_have_neither_field():
    delay = retry_delay_seconds({"Other": "1"}, fallback_delay=3.0, jitter=False)
    assert delay == 3.0


def test_retry_delay_reads_httpresponse_style_getheader():
    class _HeaderObject:
        def getheader(self, name):
            return "5" if name == "Retry-After" else None

    delay = retry_delay_seconds(_HeaderObject(), fallback_delay=1.0, jitter=False)
    assert delay == 5.0


def test_retry_delay_returns_fallback_for_object_without_get_or_getheader():
    class _NoHeaders:
        pass

    delay = retry_delay_seconds(_NoHeaders(), fallback_delay=1.5, jitter=False)
    assert delay == 1.5


def test_retry_delay_none_headers_uses_fallback():
    assert retry_delay_seconds(None, fallback_delay=4.0, jitter=False) == 4.0


def test_retry_delay_parses_http_date_retry_after():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    future = datetime.now(tz=UTC) + timedelta(seconds=30)
    delay = retry_delay_seconds(
        {"Retry-After": format_datetime(future)},
        fallback_delay=1.0,
        jitter=False,
    )
    assert delay == pytest.approx(30.0, abs=2.0)


def test_retry_delay_parses_naive_http_date_retry_after():
    from datetime import datetime, timedelta

    future = datetime.now() + timedelta(seconds=30)
    naive_rfc822 = future.strftime("%a, %d %b %Y %H:%M:%S")
    delay = retry_delay_seconds(
        {"Retry-After": naive_rfc822},
        fallback_delay=1.0,
        jitter=False,
    )
    assert delay >= 0.0


def test_retry_delay_unparseable_retry_after_falls_back_to_reset_header():
    reset_at = time.time() + 7
    delay = retry_delay_seconds(
        {"Retry-After": "not-a-date", "X-RateLimit-Reset": str(reset_at)},
        fallback_delay=1.0,
        jitter=False,
    )
    assert delay == pytest.approx(7.0, abs=1.0)


def test_retry_delay_applies_jitter_when_enabled():
    delay = retry_delay_seconds({"Retry-After": "10"}, fallback_delay=1.0, jitter=True)
    assert 10.0 <= delay <= 11.0
