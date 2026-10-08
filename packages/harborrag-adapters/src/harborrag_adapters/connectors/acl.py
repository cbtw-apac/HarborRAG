"""Source access-control evidence captured by connectors, and the fail-closed gate.

Connectors only *capture* what a source says about who may read an item, relative
to the connector's configured source scope (a Confluence space, a Jira project, a
SharePoint library). They never turn that into HarborRAG grants: effective principal
decisions are ``ResolvedPermissionSnapshot`` values imported by a trusted operator
(see ``acl_snapshots.permission_snapshot``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from .exceptions import SourceRestrictedError
from .schemas import (
    SKIP_REASON_ACCESS_RESTRICTED,
    SKIP_REASON_ACCESS_UNRESOLVED,
    ConnectorSkip,
)

AclStatus = Literal["inherited", "restricted", "unknown"]
SOURCE_ACL_METADATA_KEY = "source_acl"
RESTRICTED_CONTENT_POLICIES = frozenset({"skip", "include"})


def validate_restricted_content_policy(value: str) -> str:
    """Accept only the two supported ``restricted_content`` connector policies."""

    if value not in RESTRICTED_CONTENT_POLICIES:
        raise ValueError(
            f"restricted_content must be one of {sorted(RESTRICTED_CONTENT_POLICIES)}, "
            f"got {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class SourcePrincipal:
    """One source-side identity named by an ACL, e.g. a Confluence user or group."""

    kind: str
    id: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.id.strip():
            raise ValueError("source principal kind and id must be non-empty")

    @property
    def key(self) -> str:
        """Stable directory key, ``<kind>:<id>``."""

        return f"{self.kind}:{self.id}"

    def to_dict(self) -> dict[str, str]:
        payload = {"kind": self.kind, "id": self.id}
        if self.display_name:
            payload["display_name"] = self.display_name
        return payload


@dataclass(frozen=True, slots=True)
class SourceAcl:
    """Read access for one source item, relative to the connector's source scope.

    ``inherited``: readable by exactly the readers of the configured scope.
    ``restricted``: narrower; a reader must match at least one principal in
    *every* level (Confluence page and ancestor restrictions intersect).
    ``unknown``: access could not be determined; always handled fail-closed.
    """

    status: AclStatus
    levels: tuple[tuple[SourcePrincipal, ...], ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in get_args(AclStatus):
            raise ValueError(f"unsupported source ACL status: {self.status!r}")
        if self.status == "restricted" and not self.levels:
            raise ValueError("restricted source ACLs need at least one principal level")
        if self.status != "restricted" and self.levels:
            raise ValueError(f"{self.status} source ACLs carry no principal levels")
        if any(not level for level in self.levels):
            raise ValueError("source ACL principal levels must be non-empty")

    @classmethod
    def inherited(cls) -> SourceAcl:
        return cls("inherited")

    @classmethod
    def unknown(cls, reason: str) -> SourceAcl:
        return cls("unknown", reason=reason)

    @classmethod
    def restricted(cls, levels: Iterable[Iterable[SourcePrincipal]], *, reason: str) -> SourceAcl:
        return cls("restricted", tuple(tuple(level) for level in levels), reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "levels": [[principal.to_dict() for principal in level] for level in self.levels],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceAcl:
        levels = tuple(
            tuple(
                SourcePrincipal(
                    kind=str(item["kind"]),
                    id=str(item["id"]),
                    display_name=item.get("display_name"),
                )
                for item in level
            )
            for level in payload.get("levels") or ()
        )
        return cls(status=payload["status"], levels=levels, reason=str(payload.get("reason") or ""))


def acl_metadata(acl: SourceAcl) -> dict[str, Any]:
    """Metadata carried by records and documents whose ACL is not plain inherited."""

    if acl.status == "inherited":
        return {}
    return {SOURCE_ACL_METADATA_KEY: acl.to_dict()}


class RestrictedContentGate:
    """Apply a connector's ``restricted_content`` policy to captured ACLs.

    ``skip`` withholds restricted and unknown items. ``include`` admits restricted
    items (for ``source_acl`` corpora that import per-document snapshots) but still
    withholds unknown ones, because there is no evidence to import.
    """

    def __init__(self, policy: str) -> None:
        self._policy = validate_restricted_content_policy(policy)
        self._skips: list[ConnectorSkip] = []

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        return tuple(self._skips)

    def reset(self) -> None:
        self._skips.clear()

    def admit(self, acl: SourceAcl, *, path: str) -> bool:
        """Return whether discovery may yield the item; record a skip otherwise."""

        decision = self._decision(acl)
        if decision is None:
            return True
        reason, detail = decision
        self._skips.append(ConnectorSkip(path=path, reason=reason, detail=detail))
        return False

    def require(self, acl: SourceAcl, *, path: str) -> None:
        """Raise when describe/load reaches an item discovery would withhold."""

        decision = self._decision(acl)
        if decision is not None:
            raise SourceRestrictedError(f"{path}: {decision[1]}")

    def _decision(self, acl: SourceAcl) -> tuple[str, str] | None:
        if acl.status == "inherited":
            return None
        if acl.status == "unknown":
            return (
                SKIP_REASON_ACCESS_UNRESOLVED,
                f"source access could not be resolved ({acl.reason}); withheld to fail closed",
            )
        if self._policy == "include":
            return None
        return (
            SKIP_REASON_ACCESS_RESTRICTED,
            f"source restricts read access ({acl.reason}); restricted_content=include "
            "is only safe with source_acl corpus mode and imported snapshots",
        )
