"""What the access gates decide, and how much of the world they could see.

Access has three gates: tenant, datasource entitlement (AUTA RBAC: tenant,
project, datasource, where a datasource is a ``source_scope_id``) and source-ACL
class membership. Every gate answers with a :class:`Decision` whose
:data:`DecisionReason` says which gate spoke and why.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import model_validator

from harborrag_core.base import StrictModel
from harborrag_core.security._validation import require_text, text_items
from harborrag_core.security.classes import ClassId

DecisionReason = Literal[
    "member",
    "public",
    "tenant_shared",
    "processing_allowed",
    "not_member",
    "not_entitled",
    "no_class",
    "quarantined",
    "stale_realm",
    "tenant_mismatch",
    "unknown_snapshot",
    "processing_disallowed",
    "wrong_purpose",
]


@dataclass(frozen=True, slots=True)
class DocumentAccess:
    """What the gates need to know about one document.

    ``class_id`` is the document's ACL class; ``source_scope_id`` is the
    datasource it was read from, which AUTA entitlements are granted against.
    """

    class_id: ClassId
    source_scope_id: str

    def __post_init__(self) -> None:
        require_text(self.class_id, "document access class_id")
        require_text(self.source_scope_id, "document access source_scope_id")


@dataclass(frozen=True, slots=True)
class Decision:
    """One gate's answer for one document."""

    allowed: bool
    reason: DecisionReason
    class_id: ClassId | None = None


class AccessScope(StrictModel):
    """How completely the reader's access could be resolved.

    ``full``: every realm resolved. ``partial``: some part could not be resolved
    and ``reason`` says why. ``stale``: a directory behind the named
    ``stale_realms`` is out of date, so those realms' ACLs are not trusted. Only a
    ``stale`` scope names stale realms.
    """

    status: Literal["full", "partial", "stale"] = "full"
    reason: str | None = None
    stale_realms: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _status_carries_its_evidence(self) -> Self:
        if self.status == "partial":
            require_text(self.reason, "a partial access scope reason")
        if self.status == "stale" and not self.stale_realms:
            raise ValueError("a stale access scope must name at least one stale realm")
        if self.status != "stale" and self.stale_realms:
            raise ValueError(f"a {self.status} access scope names no stale realms")
        for realm in self.stale_realms:
            require_text(realm, "a stale realm")
        return self

    @classmethod
    def full(cls) -> AccessScope:
        return cls()

    @classmethod
    def partial(cls, reason: str) -> AccessScope:
        return cls(status="partial", reason=reason)

    @classmethod
    def stale(cls, realms: Iterable[str]) -> AccessScope:
        """A stale scope naming ``realms``; a bare string is refused, not split into characters."""

        return cls(status="stale", stale_realms=text_items(realms, "a stale realm"))
