"""Adapter failure logging: the backend and the vendor's HTTP status, never the error's text.

Adapter code writes events with ``harborrag_core.observability.log_events``. This module
adds what only adapter code knows: which backend failed, and the HTTP status a vendor
exception keeps on its response, as ``requests`` and ``httpx`` errors do. Like all of
``_shared``, it imports only core and the standard library.

Typical usage example:

    except Exception as error:
        log_adapter_failure(logger, "connector.rate_limiter_fallback", error, "redis")
"""

from __future__ import annotations

import logging

from harborrag_core.observability.log_events import STATUS_CODE_FIELD, FailureKind, log_failure

BACKEND_FIELD = "backend"
_HTTP_STATUSES = range(100, 600)


def vendor_status_code(error: BaseException) -> int | None:
    """Return the HTTP status a vendor exception keeps on its response, or None.

    Args:
        error: A vendor exception, such as a ``requests`` or ``httpx`` HTTP error.

    Returns:
        ``error.response.status_code`` when it is an HTTP status, else None.
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and status in _HTTP_STATUSES:
        return status
    return None


def log_adapter_failure(
    logger: logging.Logger,
    event: str,
    error: BaseException,
    backend: str,
    kind: FailureKind = FailureKind.CLASSIFIED,
    **fields: object,
) -> None:
    """Log a failure in adapter code with its backend and the vendor's HTTP status.

    Args:
        logger: The adapter module's logger.
        event: Dotted lower-case event name, such as ``connector.callback_failed``.
        error: The exception the adapter caught.
        backend: What failed, such as ``redis``, ``sharepoint`` or ``qdrant``.
        kind: Whose failure it is, which sets the level and the traceback.
        **fields: IDs, counts, codes, reasons and durations, as for ``log_event``.
    """
    described: dict[str, object] = {BACKEND_FIELD: backend}
    status = vendor_status_code(error)
    if status is not None:
        described[STATUS_CODE_FIELD] = status
    log_failure(logger, event, error, kind, 2, **described, **fields)
