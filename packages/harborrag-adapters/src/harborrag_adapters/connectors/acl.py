"""Source access-control evidence and the fail-closed restricted-content gate.

Handles:
- Re-exported ACL evidence types from ``harborrag_core.sources.acl``
- Validation of the ``restricted_content`` policy
- Admission and skip recording for restricted or unresolved items

Does NOT compute HarborRAG grants; that happens outside connectors.
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
    """Return ``value`` if it is ``skip`` or ``include``, else raise ``ValueError``."""
    if value not in RESTRICTED_CONTENT_POLICIES:
        raise ValueError(
            f"restricted_content must be one of {sorted(RESTRICTED_CONTENT_POLICIES)}, "
            f"got {value!r}"
        )
    return value


class RestrictedContentGate:
    """Gate that applies a connector's ``restricted_content`` policy to captured ACLs.

    ``skip`` withholds restricted and unknown items; ``include`` admits restricted
    items but still withholds unknown ones.
    """

    def __init__(self, policy: str) -> None:
        self._policy = validate_restricted_content_policy(policy)
        self._skips: list[ConnectorSkip] = []

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return the skips recorded by ``admit`` since the last ``reset``."""
        return tuple(self._skips)

    def reset(self) -> None:
        """Forget recorded skips. Call at the start of each discovery traversal."""
        self._skips.clear()

    def admit(self, acl: SourceAcl, *, path: str) -> bool:
        """Return whether discovery may yield the item, recording a skip otherwise.

        Args:
            acl: Captured access evidence for the item.
            path: Item path used in the recorded skip.

        Returns:
            ``True`` if the item is admitted, ``False`` if it was withheld.
        """
        decision = self._decision(acl)
        if decision is None:
            return True
        reason, detail = decision
        self._skips.append(ConnectorSkip(path=path, reason=reason, detail=detail))
        return False

    def require(self, acl: SourceAcl, *, path: str) -> None:
        """Raise when describe or load reaches an item discovery would withhold.

        Args:
            acl: Captured access evidence for the item.
            path: Item path used in the error message.

        Raises:
            SourceRestrictedError: If the policy withholds the item.
        """
        decision = self._decision(acl)
        if decision is not None:
            raise SourceRestrictedError(f"{path}: {decision[1]}")

    def _decision(self, acl: SourceAcl) -> tuple[str, str] | None:
        """Return ``(reason, detail)`` if the item must be withheld, else ``None``."""
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
