"""References to the retrievable things access is enforced on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from harborrag_core.security._validation import require_text

ResourceKind = Literal["document", "chunk", "node", "edge", "summary", "blob", "memory"]

_KINDS: frozenset[str] = frozenset(get_args(ResourceKind))


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """One resource, and the document it derives from when it is not a document."""

    kind: ResourceKind
    id: str
    document_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise ValueError(f"unsupported resource kind: {self.kind!r}")
        require_text(self.id, "resource id")
        if self.document_id is not None:
            require_text(self.document_id, "resource document_id")
