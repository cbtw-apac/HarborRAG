"""Response shapes for `GET /v1/admin/failures`."""

from __future__ import annotations

from datetime import datetime

from harborrag_app.api.schemas import ApiModel


class DocumentFailureOut(ApiModel):
    document_id: str
    document_version_id: str
    failed_stage: str
    category: str
    retryable: bool
    safe_error_code: str
    attempts: int
    first_failure_at: datetime
    last_failure_at: datetime


class DocumentFailurePage(ApiModel):
    items: list[DocumentFailureOut]
