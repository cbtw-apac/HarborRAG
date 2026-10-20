"""What a non-2xx response means, decided once as a value.

:func:`classify` turns the facts of a failed response into a
:class:`Resolution`: an :class:`Action` for the HTTP client to take and the
error to raise when that action cannot proceed. The client is the only code
that acts on it, so retries, throttling, and refresh live in one loop.

A vendor kit adds :data:`ResponseRule` functions for its quirks (a Data
Center pagination 400 worth retrying, a ``RateLimit-Reason`` header that
means throttling) instead of writing its own retry loop. Rules run in order
and the first non-``None`` answer wins; a 401 is decided before any rule, so
no rule can mask a rejected credential.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from harborrag_core.sources import AuthError, Forbidden, ItemNotFound, Transient

from .errors import UnexpectedStatus
from .retry import RetryPolicy


class Action(StrEnum):
    """What the HTTP client does with a failed response."""

    RETRY = "retry"
    """Back off and try again (idempotent requests only)."""
    THROTTLED = "throttled"
    """Wait as the server asks; when attempts run out, raise ``RateLimited``."""
    REFRESH = "refresh"
    """Refresh credentials once and repeat the request."""
    FAIL = "fail"
    """Raise the error now."""


@dataclass(frozen=True)
class ResponseFacts:
    """The parts of a failed response a classification may look at.

    ``headers`` is the response's ``httpx.Headers`` (case-insensitive) when the
    client builds the facts; ``detail`` is a short, whitespace-collapsed prefix
    of the body; ``where`` is ``"<METHOD> <redacted URL>"`` for messages.
    """

    status_code: int
    headers: Mapping[str, str]
    detail: str
    where: str


@dataclass(frozen=True)
class Resolution:
    """An action and the error raised if the action cannot proceed."""

    action: Action
    error: Exception


type ResponseRule = Callable[[ResponseFacts], Resolution | None]


def classify(facts: ResponseFacts, rules: Sequence[ResponseRule] = ()) -> Resolution:
    """Decide what a non-2xx response means: 401, then ``rules``, then defaults."""

    if facts.status_code == 401:
        return Resolution(Action.REFRESH, AuthError(f"{_message(facts)}: credentials rejected"))
    for rule in rules:
        resolution = rule(facts)
        if resolution is not None:
            return resolution
    return _default(facts)


def _default(facts: ResponseFacts) -> Resolution:
    if _throttled(facts):
        return Resolution(Action.THROTTLED, Transient(f"{_message(facts)}: throttled"))
    if RetryPolicy.retryable_status(facts.status_code):
        return Resolution(Action.RETRY, _status_error(facts))
    return Resolution(Action.FAIL, _status_error(facts))


def _throttled(facts: ResponseFacts) -> bool:
    if facts.status_code == 429:
        return True
    # GitHub signals both primary and secondary rate limits with 403.
    return facts.status_code == 403 and (
        facts.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in facts.headers
    )


def _message(facts: ResponseFacts) -> str:
    return f"{facts.status_code} for {facts.where}"


def _status_error(facts: ResponseFacts) -> Exception:
    status, detail, message = facts.status_code, facts.detail, _message(facts)
    if status == 403:
        return Forbidden(f"{message}: access denied")
    if status in (404, 410):
        return ItemNotFound(message)
    if status >= 500:
        return Transient(f"{message}: server error")
    return UnexpectedStatus(status, f"{message}: {detail}" if detail else message, detail)
