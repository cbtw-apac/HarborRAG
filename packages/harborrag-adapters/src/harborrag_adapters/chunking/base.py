from __future__ import annotations

from abc import ABC, abstractmethod
from importlib.util import find_spec
from typing import ClassVar

from harborrag_core.contracts.chunking import (
    JsonStructureSplitRequest,
    StructureSplitRequest,
    TextRefinementRequest,
    TextSplit,
    TokenCounter,
)

ChunkRequest = TextRefinementRequest | StructureSplitRequest | JsonStructureSplitRequest


class HarborBaseChunk[ChunkRequestT: ChunkRequest](ABC):
    """Shared contract for provider-backed chunking adapters.

    Concrete adapters advertise a stable registry name and the request type
    accepted by :meth:`split`. Document-level chunking policy remains in the
    engine; adapters only translate one request into HarborRAG ``TextSplit``
    values.
    """

    chunk_name: ClassVar[str] = "base"
    required_dependency: ClassVar[str | None] = None
    request_type: ClassVar[type[object]] = object

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    @property
    def name(self) -> str:
        """Return the stable name used by the chunk adapter registry."""

        return self.chunk_name

    def supports(self, request: object) -> bool:
        """Return whether this adapter accepts ``request``."""

        return isinstance(request, self.request_type)

    @classmethod
    def available(cls) -> bool:
        """Return whether the adapter's optional runtime dependency is installed."""

        if cls.required_dependency is None:
            return True
        try:
            return find_spec(cls.required_dependency) is not None
        except (ImportError, AttributeError, ValueError):
            return False

    @abstractmethod
    def split(self, request: ChunkRequestT) -> tuple[TextSplit, ...]:
        """Return ordered HarborRAG-owned splits for one adapter request."""

        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"
