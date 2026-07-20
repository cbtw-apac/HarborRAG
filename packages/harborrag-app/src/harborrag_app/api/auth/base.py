"""Token verifier base class (ST4): base/mock pair per repo convention."""

from __future__ import annotations

from abc import ABC, abstractmethod

from harborrag_app.api.auth.principal import Principal


class BaseTokenVerifier(ABC):
    """Verify a bearer token string and return the authenticated Principal.

    Implementations raise HarborAuthError for every failure mode (missing
    claims, bad signature, expiry) — never framework exceptions.
    """

    @abstractmethod
    def verify(self, token: str) -> Principal:
        """Return the Principal encoded in the token, or raise HarborAuthError."""
        raise NotImplementedError
