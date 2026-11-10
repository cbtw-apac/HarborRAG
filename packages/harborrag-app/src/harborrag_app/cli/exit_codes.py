"""The CLI's exit codes: one per class of HTTP status, plus usage, failed runs and Ctrl-C.

Every command exits through ``exit_code_for`` or one of the constants here, so a script
reads the same outcome from the CLI as from the route's status.
"""

from __future__ import annotations

USAGE = 2
RUN_FAILED = 8
INTERRUPTED = 130

_OTHER_FAILURE = 1
_BY_STATUS: dict[int, int] = {
    400: USAGE,
    413: USAGE,
    422: USAGE,
    501: USAGE,
    404: 3,
    409: 4,
    412: 4,
    401: 5,
    403: 5,
    429: 6,
    503: 6,
    504: 6,
}


def exit_code_for(status: int) -> int:
    """Return the exit code of an outcome with HTTP ``status``.

    Args:
        status: Status the route answers, or would answer, for the same outcome.

    Returns:
        ``0`` for 2xx; ``2`` bad input or unsupported; ``3`` not found; ``4`` conflict;
        ``5`` not allowed; ``6`` unavailable; ``1`` for anything else.
    """
    if 200 <= status < 300:
        return 0
    return _BY_STATUS.get(status, _OTHER_FAILURE)
