"""Deterministic token verifier for tests and local wiring (ST4)."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_core.contracts.errors import HarborAuthError


@dataclass(slots=True)
class MockTokenVerifier(BaseTokenVerifier):
    """Accepts tokens of the form 'mock-<role>' and nothing else."""

    subject: str = "mock-user"

    def verify(self, token: str) -> Principal:
        """Map 'mock-<role>' to a Principal; any other token is rejected."""
        prefix, _, role = token.partition("-")
        if prefix != "mock" or role not in ROLE_ORDER:
            raise HarborAuthError("invalid mock token")
        return Principal(subject=self.subject, role=role, token_kind="mock")
