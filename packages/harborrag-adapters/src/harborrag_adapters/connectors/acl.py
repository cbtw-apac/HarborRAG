"""Source access-control evidence captured by connectors, and the fail-closed gate.

Connectors only *capture* what a source says about who may read an item, relative
to the connector's configured source scope (a Confluence space, a Jira project, a
SharePoint library). They never turn that into HarborRAG grants: effective
decisions are made elsewhere, from this evidence plus a directory of the source's
own identities. The evidence types themselves live in
``harborrag_core.sources.acl`` and are re-exported here for connector code.
"""

from __future__ import annotations

from harborrag_core.sources.acl import (
    SOURCE_ACL_METADATA_KEY,
    AclStatus,
    SourceAcl,
    SourcePrincipal,
    acl_metadata,
)

from .exceptions import SourceRestrictedError
from .schemas import (
    SKIP_REASON_ACCESS_RESTRICTED,
    SKIP_REASON_ACCESS_UNRESOLVED,
    ConnectorSkip,
)

__all__ = [
    "RESTRICTED_CONTENT_POLICIES",
    "SOURCE_ACL_METADATA_KEY",
    "AclStatus",
    "RestrictedContentGate",
    "SourceAcl",
    "SourcePrincipal",
    "acl_metadata",
    "validate_restricted_content_policy",
]

RESTRICTED_CONTENT_POLICIES = frozenset({"skip", "include"})


def validate_restricted_content_policy(value: str) -> str:
    """Accept only the two supported ``restricted_content`` connector policies."""

    if value not in RESTRICTED_CONTENT_POLICIES:
        raise ValueError(
            f"restricted_content must be one of {sorted(RESTRICTED_CONTENT_POLICIES)}, "
            f"got {value!r}"
        )
    return value


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
            "needs source_acl corpus mode and imported snapshots, and even then graph "
            "source-scope nodes expose its title and attributes to every scope reader",
        )
