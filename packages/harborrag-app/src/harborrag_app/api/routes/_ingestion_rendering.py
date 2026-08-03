"""Shared AppResponse -> JSONResponse rendering for Temporal-backed routes.

Used by both ingestions.py (/ingestions) and jobs.py (/jobs), which drive the
same underlying start_ingestion/ingestion_status/ingestion_result/
control_ingestion calls and so hit the same runtime error types.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from harborrag_app.api.errors import error_envelope
from harborrag_app.workflow_control import AppResponse
from harborrag_runtime.errors import (
    WorkflowNotFoundError,
    WorkflowNotRetryableError,
    WorkflowNotRunningError,
    WorkflowRunAlreadyStartedError,
)

logger = logging.getLogger("harborrag.app.api.ingestion_rendering")

# Failures the caller can act on, rather than upstream faults. Anything absent
# from this map is a 502: the application service only surfaces failures that
# originate behind this boundary, and reporting those as client errors would
# send operators looking in the wrong place.
_CLIENT_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    WorkflowNotFoundError.__name__: (404, "ingestion_run_not_found"),
    WorkflowNotRunningError.__name__: (409, "ingestion_run_not_running"),
    WorkflowNotRetryableError.__name__: (409, "ingestion_artifacts_not_retryable"),
    WorkflowRunAlreadyStartedError.__name__: (409, "ingestion_run_already_started"),
}


def render_ingestion_response(
    request: Request,
    response: AppResponse,
    *,
    operation: str,
    success_status: int = 200,
) -> JSONResponse:
    """Map an application response onto an HTTP status and error envelope.

    A run the caller named but Temporal does not have is a 404, and a run whose
    state conflicts with the request -- already finished, or a run ID already in
    use -- is a 409. Reporting either as a 502 sends operators looking for a
    broken Temporal cluster.
    """

    if response.ok:
        logger.info("Ingestion operation completed", extra={"operation": operation})
        return JSONResponse(status_code=success_status, content=response.data)

    error_type = str(response.data.get("error_type", "runtime_operation_failed"))
    status_code, code = _CLIENT_ERROR_RESPONSES.get(
        error_type,
        (502, "ingestion_operation_failed"),
    )
    logger.warning(
        "Ingestion operation failed",
        extra={"operation": operation, "error_type": error_type, "status_code": status_code},
    )
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(
            request,
            code,
            response.error or "Ingestion operation failed",
            {"operation": operation, "error_type": error_type},
        ),
    )
