from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_core.schemas.storage import StorageOperationContext


class FalkorGenerationMixin:
    """Apply graph generation visibility changes to nodes and edges."""

    async def _write(self, statement: str, parameters: Mapping[str, Any]) -> None:
        raise NotImplementedError

    async def _set_generation_state(
        self,
        artifact_id: str,
        generation_id: str,
        *,
        index_state: str,
        is_active: bool,
        context: StorageOperationContext,
    ) -> None:
        parameters = {
            "tenant_id": str(context.tenant_id),
            "artifact_id": artifact_id,
            "generation_id": generation_id,
            "index_state": index_state,
            "is_active": is_active,
        }
        await self._write(
            """
            MATCH (n:HarborEntity)
            WHERE n.tenant_id = $tenant_id
              AND n.artifact_id = $artifact_id
              AND n.generation_id = $generation_id
            SET n.index_state = $index_state, n.is_active = $is_active
            """,
            parameters,
        )
        await self._write(
            """
            MATCH ()-[r]->()
            WHERE r.tenant_id = $tenant_id
              AND r.artifact_id = $artifact_id
              AND r.generation_id = $generation_id
            SET r.index_state = $index_state, r.is_active = $is_active
            """,
            parameters,
        )
