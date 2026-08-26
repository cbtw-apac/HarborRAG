"""Token-verifier double for the app API tests."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_core.contracts.errors import HarborAuthError


@dataclass(slots=True)
class MockTokenVerifier(BaseTokenVerifier):
    """Accept tokens of the form ``mock-<role>`` for auth boundary tests."""

    subject: str = "mock-user"

    def verify(self, token: str) -> Principal:
        prefix, _, role = token.partition("-")
        if prefix != "mock" or role not in ROLE_ORDER:
            raise HarborAuthError("invalid mock token")
        return Principal(
            subject=self.subject,
            role=role,
            tenant_ids=frozenset({"*"}),
            token_kind="mock",
        )


__all__ = ["MockTokenVerifier"]
