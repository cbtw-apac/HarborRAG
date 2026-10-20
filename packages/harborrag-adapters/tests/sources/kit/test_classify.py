from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from harborrag_adapters.sources._kit.classify import (
    Action,
    Resolution,
    ResponseFacts,
    classify,
)
from harborrag_adapters.sources._kit.errors import UnexpectedStatus
from harborrag_adapters.sources._kit.net import redact_url
from harborrag_core.sources import AuthError, Forbidden, ItemNotFound, Transient


def facts(
    status: int, *, headers: Mapping[str, str] | None = None, detail: str = ""
) -> ResponseFacts:
    return ResponseFacts(status, headers or {}, detail, "GET https://h.example/x")


@pytest.mark.parametrize(
    ("status", "headers", "action", "error"),
    [
        (401, {}, Action.REFRESH, AuthError),
        (429, {}, Action.THROTTLED, Transient),
        (403, {"x-ratelimit-remaining": "0"}, Action.THROTTLED, Transient),
        (403, {"retry-after": "5"}, Action.THROTTLED, Transient),
        (403, {}, Action.FAIL, Forbidden),
        (404, {}, Action.FAIL, ItemNotFound),
        (410, {}, Action.FAIL, ItemNotFound),
        (503, {}, Action.RETRY, Transient),
        (501, {}, Action.FAIL, Transient),
        (422, {}, Action.FAIL, UnexpectedStatus),
    ],
)
def test_default_classification(
    status: int, headers: dict[str, str], action: Action, error: type[Exception]
) -> None:
    resolution = classify(facts(status, headers=headers))
    assert resolution.action is action
    assert type(resolution.error) is error
    assert str(status) in str(resolution.error)


def test_unexpected_status_keeps_code_and_detail() -> None:
    error = classify(facts(400, detail="bad jql")).error
    assert isinstance(error, UnexpectedStatus)
    assert (error.status_code, error.detail) == (400, "bad jql")
    assert str(error) == "400 for GET https://h.example/x: bad jql"


def test_headers_are_read_case_insensitively_from_httpx_headers() -> None:
    headers = httpx.Headers({"X-RateLimit-Remaining": "0"})
    assert classify(facts(403, headers=headers)).action is Action.THROTTLED


def test_rule_runs_first_and_none_falls_through() -> None:
    def quirk(f: ResponseFacts) -> Resolution | None:
        if f.status_code == 400 and "paging" in f.detail:
            return Resolution(Action.RETRY, Transient("dc paging"))
        return None

    assert classify(facts(400, detail="paging bug"), [quirk]).action is Action.RETRY
    assert classify(facts(400, detail="bad jql"), [quirk]).action is Action.FAIL


def test_first_matching_rule_wins() -> None:
    first = Resolution(Action.THROTTLED, Transient("first"))
    second = Resolution(Action.FAIL, Transient("second"))
    assert classify(facts(400), [lambda f: None, lambda f: first, lambda f: second]) is first


def test_rule_cannot_override_401() -> None:
    def rule(f: ResponseFacts) -> Resolution:
        return Resolution(Action.RETRY, Transient("x"))

    assert classify(facts(401), [rule]).action is Action.REFRESH


def test_redact_url() -> None:
    assert redact_url("https://u:p@h.example:8443/a/b?token=s#f") == "https://h.example:8443/a/b"
    assert redact_url(httpx.URL("https://h.example/a?sig=1")) == "https://h.example/a"
    assert redact_url("/x?token=s#frag") == "/x"


def test_redact_url_never_raises_on_an_unparseable_url() -> None:
    assert redact_url("https://u:p@[::1/a?token=s#f") == "https://[::1/a"
