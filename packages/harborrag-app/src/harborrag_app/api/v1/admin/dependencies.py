"""Dependencies shared by the ``/v1/admin`` routes."""

from __future__ import annotations

from typing import Any

from harborrag_app.api.schemas import Error


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entries for error responses on the shared ``Error`` schema."""
    return {code: {"model": Error} for code in codes}
