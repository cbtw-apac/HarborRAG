from __future__ import annotations

from typing import TypeVar
from uuid import UUID, uuid4

from pydantic_core import core_schema

TId = TypeVar("TId", bound="HarborId")


class HarborId(str):
    """Runtime-distinct, JSON-compatible identifier used across storage systems."""

    @classmethod
    def new(cls: type[TId]) -> TId:
        return cls(str(uuid4()))

    @classmethod
    def from_uuid(cls: type[TId], value: UUID) -> TId:
        return cls(str(value))

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: object,
    ) -> core_schema.CoreSchema:
        del source_type, handler
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1, strip_whitespace=True),
        )


class TenantId(HarborId):
    """Identifies one tenant across HarborRAG stores."""


class DataSourceId(HarborId):
    """Identifies one data source across HarborRAG stores."""


class DocumentId(HarborId):
    """Identifies one document across HarborRAG stores."""


class DocumentVersionId(HarborId):
    """Identifies one document version across HarborRAG stores."""


class ChunkId(HarborId):
    """Identifies one chunk across HarborRAG stores."""


class EntityId(HarborId):
    """Identifies one entity across HarborRAG stores."""


class RelationshipId(HarborId):
    """Identifies one relationship across HarborRAG stores."""


class WorkflowId(HarborId):
    """Identifies one workflow across HarborRAG stores."""


class CheckpointId(HarborId):
    """Identifies one checkpoint across HarborRAG stores."""
