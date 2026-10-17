"""ACL classes: the unit of source-ACL membership.

Every document belongs to exactly one class in its tenant. A class is a
distinct, known :class:`~harborrag_core.sources.acl.SourceAcl`, so membership is
decided once per class rather than once per document. An ``unknown`` ACL is
never a class: it cannot be evaluated, so its documents are quarantined instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, NewType

from harborrag_core.security._validation import require_text

if TYPE_CHECKING:
    from harborrag_core.sources.acl import SourceAcl

ClassId = NewType("ClassId", str)

QUARANTINE_CLASS = ClassId("quarantine")
"""The class of a document whose ACL is unknown. Nobody is ever a member."""


@cache
def _source_acl_type() -> type:
    """``SourceAcl``, imported on first use to break an import cycle.

    ``harborrag_core.sources`` imports ``domain``, whose validation imports this
    package, so importing ``SourceAcl`` at module scope would loop back into a
    half-initialised ``harborrag_core.security``.
    """

    from harborrag_core.sources.acl import SourceAcl

    return SourceAcl


@dataclass(frozen=True, slots=True)
class AclClass:
    """One distinct, known ACL within a tenant, named by ``class_id``."""

    tenant_id: str
    class_id: ClassId
    acl: SourceAcl

    def __post_init__(self) -> None:
        require_text(self.tenant_id, "ACL class tenant_id")
        require_text(self.class_id, "ACL class class_id")
        if not isinstance(self.acl, _source_acl_type()):
            raise ValueError("ACL class acl must be a SourceAcl")
        if self.acl.status == "unknown":
            raise ValueError("an unknown source ACL cannot be an ACL class")
