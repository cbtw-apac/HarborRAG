from __future__ import annotations

from abc import ABC, abstractmethod

from harborrag_core.domain.document import Document


class BaseIndexer(ABC):
    """Index normalized documents into repositories.

    TODO: Implement embedding batch creation, idempotent vector upserts, graph writes,
    artifact writes, and checkpoint updates in a transaction-like order.
    """

    @abstractmethod
    def index(self, documents: list[Document]) -> int:
        raise NotImplementedError
