"""Shared SQLAlchemy persistence used by relational database providers."""

from harborrag_adapters.repositories.database.sqlalchemy.backend import (
    SQLAlchemyDatabaseBackend,
    SQLAlchemyUnitOfWork,
    SQLAlchemyUnitOfWorkFactory,
)
from harborrag_adapters.repositories.database.sqlalchemy.chunks import (
    SQLChunkRepository,
    SQLOutboxRepository,
)
from harborrag_adapters.repositories.database.sqlalchemy.dialects import (
    insert_for_dialect,
)
from harborrag_adapters.repositories.database.sqlalchemy.documents import (
    SQLDocumentRepository,
)

__all__ = [
    "SQLAlchemyDatabaseBackend",
    "SQLAlchemyUnitOfWork",
    "SQLAlchemyUnitOfWorkFactory",
    "SQLChunkRepository",
    "SQLDocumentRepository",
    "SQLOutboxRepository",
    "insert_for_dialect",
]
