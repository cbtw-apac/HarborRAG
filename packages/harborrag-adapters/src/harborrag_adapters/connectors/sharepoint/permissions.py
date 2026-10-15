"""Least-privilege checks on Microsoft Graph application and delegated tokens."""

from __future__ import annotations

import base64
import json
from typing import Any

from harborrag_adapters.connectors.exceptions import AuthenticationError

# Application roles that grant access beyond the sites an admin selected. The
# connector must run on ``Sites.Selected`` so it can only reach granted sites.
BROAD_GRAPH_ROLES = frozenset(
    {
        "Sites.Read.All",
        "Sites.ReadWrite.All",
        "Sites.Manage.All",
        "Sites.FullControl.All",
        "Files.Read.All",
        "Files.ReadWrite.All",
    }
)


def token_claims(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying it, or return ``None``.

    The claims are only inspected to refuse over-privileged tokens; Microsoft
    Graph still validates the token on every request.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeError):
        return None
    return claims if isinstance(claims, dict) else None


def reject_broad_permissions(token: str) -> None:
    """Refuse a token whose application roles exceed ``Sites.Selected``.

    A token that is not a decodable JWT carries nothing to inspect, so it is
    let through for Graph to judge.
    """
    claims = token_claims(token)
    if claims is None:
        return
    roles = claims.get("roles")
    if not isinstance(roles, list):
        return
    broad = sorted(BROAD_GRAPH_ROLES.intersection(str(role) for role in roles))
    if broad:
        raise AuthenticationError(
            "SharePoint app registration has broad Microsoft Graph permissions "
            f"{', '.join(broad)}; grant Sites.Selected for the configured sites instead"
        )


def reject_broad_delegated_scopes(token: str) -> None:
    """Refuse a delegated token whose scopes exceed ``Sites.Selected``.

    Delegated tokens carry consented permissions in the space-separated
    ``scp`` claim rather than ``roles``. Only tokens the connector obtains
    itself are checked, so a hand-supplied ``access_token`` keeps working.
    """
    claims = token_claims(token)
    if claims is None:
        return
    scopes = claims.get("scp")
    if not isinstance(scopes, str):
        return
    broad = sorted(BROAD_GRAPH_ROLES.intersection(scopes.split()))
    if broad:
        raise AuthenticationError(
            "SharePoint app registration has broad delegated Microsoft Graph permissions "
            f"{', '.join(broad)}; consent Sites.Selected for the configured sites instead"
        )
