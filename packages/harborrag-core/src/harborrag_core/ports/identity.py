"""Who a request says it is, after its credential has been verified."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harborrag_core.security._validation import require_text, text_items


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """The claims of a credential whose signature and lifetime have been checked.

    ``subject`` is the credential's subject, ``user_id`` the HarborRAG principal
    it authenticates, ``role`` its role claim and ``tenant_ids`` the tenants it
    may act in. ``actor_id`` names who is acting when that is someone other than
    ``user_id``, and ``token_kind`` says what sort of credential it was.
    """

    subject: str
    user_id: str
    role: str
    tenant_ids: frozenset[str]
    actor_id: str | None = None
    token_kind: str = "jwt"

    def __post_init__(self) -> None:
        require_text(self.subject, "verified token subject")
        require_text(self.user_id, "verified token user_id")
        require_text(self.role, "verified token role")
        require_text(self.token_kind, "verified token token_kind")
        if self.actor_id is not None:
            require_text(self.actor_id, "verified token actor_id")
        object.__setattr__(
            self, "tenant_ids", frozenset(text_items(self.tenant_ids, "verified token tenant id"))
        )


class TokenVerifierPort(Protocol):
    """Verify a bearer credential; raise rather than return a partial identity."""

    def verify(self, token: str) -> VerifiedToken: ...
