"""Mint a short-lived owner token for the local development API.

Run inside the API container through ``scripts/deployment/dev.sh token``. It
signs with the container's own ``HARBORRAG_AUTH_SECRET`` and refuses unless the
process is ``HARBORRAG_ENV=dev`` with ``HARBORRAG_AUTH_MODE=hmac``, so it can
never become a production credential issuer.
"""

from __future__ import annotations

import sys
import time

import jwt
from pydantic import ValidationError

from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborConfigurationError

DEV_TOKEN_SUBJECT = "local-developer"
# Same floor build_token_verifier enforces for HMAC secrets.
MIN_SECRET_BYTES = 32


def mint_dev_token(settings: ApiSettings, *, now: int | None = None) -> str:
    """Return an HS256 owner token for every tenant, valid for the max lifetime."""

    if settings.env != "dev":
        raise HarborConfigurationError(
            "development tokens are available only when HARBORRAG_ENV=dev"
        )
    if settings.auth_mode != "hmac" or settings.auth_secret is None:
        raise HarborConfigurationError(
            "development tokens require HARBORRAG_AUTH_MODE=hmac and HARBORRAG_AUTH_SECRET"
        )
    secret = settings.auth_secret.get_secret_value()
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise HarborConfigurationError(
            f"HARBORRAG_AUTH_SECRET must contain at least {MIN_SECRET_BYTES} UTF-8 bytes"
        )
    issued_at = int(time.time()) if now is None else now
    claims: dict[str, object] = {
        "sub": DEV_TOKEN_SUBJECT,
        "role": "owner",
        "tenants": ["*"],
        "iat": issued_at,
        "exp": issued_at + settings.auth_max_token_lifetime_seconds,
        "iss": settings.auth_issuer,
        "aud": settings.auth_audience,
    }
    claims.setdefault(settings.auth_user_id_claim, DEV_TOKEN_SUBJECT)
    return jwt.encode(claims, secret, algorithm="HS256")


def main() -> int:
    try:
        token = mint_dev_token(ApiSettings())
    except ValidationError as exc:
        # Never echo input values: a malformed secret would appear in them.
        fields = ", ".join(sorted({".".join(map(str, e["loc"])) for e in exc.errors()}))
        print(f"error: invalid API settings: {fields}", file=sys.stderr)
        return 1
    except HarborConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
