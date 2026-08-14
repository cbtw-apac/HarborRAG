"""Small standard-library client for release-owned GitHub API calls."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .diagnostics import redact_diagnostic

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GitHubRequestError(RuntimeError):
    """Raised when GitHub cannot return an HTTP response."""


@dataclass(frozen=True, slots=True)
class GitHubResponse:
    """Status, decoded JSON payload, and bounded response text."""

    status_code: int
    payload: object | None
    text: str


def _decode_response(raw: bytes) -> tuple[object | None, str]:
    """Decode a response body as JSON while retaining its diagnostic text."""

    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return None, text


def github_request(
    repository: str,
    endpoint: str,
    token: str,
    *,
    query: dict[str, str | int] | None = None,
    payload: dict[str, object] | None = None,
) -> GitHubResponse:
    """Call a repository-scoped GitHub endpoint on the fixed API origin."""

    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("GitHub repository must use the owner/name form")
    path = quote(endpoint.strip("/"), safe="/")
    url = f"https://api.github.com/repos/{repository}/{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            decoded, text = _decode_response(response.read())
            return GitHubResponse(response.status, decoded, text)
    except HTTPError as exc:
        decoded, text = _decode_response(exc.read())
        return GitHubResponse(exc.code, decoded, text)
    except OSError as exc:
        raise GitHubRequestError(redact_diagnostic(exc)) from exc
