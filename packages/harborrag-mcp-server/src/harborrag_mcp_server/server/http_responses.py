"""Shared JSON and browser response helpers for the MCP HTTP boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.responses import JSONResponse


def configuration_response(description: dict[str, object]) -> JSONResponse:
    from starlette.responses import JSONResponse

    return JSONResponse(description, headers={"Cache-Control": "no-store"})


def error_response(
    message: str,
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"error": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def browser_security_headers(nonce: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; connect-src 'self'; "
            f"script-src 'nonce-{nonce}'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


__all__ = ["browser_security_headers", "configuration_response", "error_response"]
