"""Runtime adapter exposing retrieval façades as bounded agent read tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, cast

from harborrag_core.chunking import RelationType
from harborrag_core.retrieval import (
    GraphDirection,
    GraphNeighborhoodQuery,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
    compact_node,
    compact_path,
    compact_relation,
    compact_triplet,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_engine.agent import AgentToolSpec
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.agent.tool_specs import RUNTIME_AGENT_TOOL_SPECS
from harborrag_runtime.contracts import (
    GraphNeighborhoodRequest,
    GraphPathRequest,
    GraphSubgraphRequest,
    GraphTripletRequest,
    RetrievalRequest,
)

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG


@dataclass(slots=True)
class RuntimeAgentToolProvider:
    """Translate engine tool calls into the shared runtime SDK façades."""

    runtime: HarborRAG

    def list_tools(self, tenant_id: str | None = None) -> list[AgentToolSpec]:
        del tenant_id
        return [cast("AgentToolSpec", spec) for spec in RUNTIME_AGENT_TOOL_SPECS]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        values = dict(arguments or {})
        try:
            access = _access(values, principal_id)
            operations = {
                "vector_search": self._vector,
                "graph_neighborhood": self._neighborhood,
                "graph_triplet_search": self._triplets,
                "graph_path_search": self._paths,
                "graph_subgraph_search": self._subgraph,
            }
            operation = operations.get(name)
            if operation is None:
                return {"ok": False, "error": "agent tool is not available"}
            return await operation(values, access)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "agent retrieval tool failed"}

    async def _vector(
        self,
        values: dict[str, object],
        access: AccessContext,
    ) -> dict[str, object]:
        lane = RetrievalLane(_optional_text(values, "lane") or RetrievalLane.HYBRID.value)
        response = await self.runtime.retrieval.search(
            RetrievalRequest(
                access=access,
                query=_text(values, "query"),
                top_k=_integer(values, "top_k", default=5, maximum=20),
                filters=_mapping(values, "filters"),
                lane=lane,
                observe_graph=False,
            )
        )
        return {
            "ok": True,
            "results": [asdict(item) for item in response.results],
            "diagnostics": response.diagnostics,
        }

    async def _triplets(
        self,
        values: dict[str, object],
        access: AccessContext,
    ) -> dict[str, object]:
        predicate = _optional_text(values, "predicate")
        response = await self.runtime.graph.search_triplets(
            GraphTripletRequest(
                access=access,
                query=GraphTripletQuery(
                    subject=_optional_text(values, "subject"),
                    predicate=RelationType(predicate) if predicate is not None else None,
                    object=_optional_text(values, "object"),
                    limit=_integer(values, "limit", default=10, maximum=20),
                ),
            )
        )
        return {
            "ok": True,
            "triplets": [compact_triplet(item) for item in response.triplets],
            "diagnostics": response.diagnostics,
        }

    async def _neighborhood(
        self,
        values: dict[str, object],
        access: AccessContext,
    ) -> dict[str, object]:
        response = await self.runtime.graph.neighborhood(
            GraphNeighborhoodRequest(
                access=access,
                query=GraphNeighborhoodQuery(
                    query=_text(values, "query"),
                    seed_limit=_integer(values, "seed_limit", default=3, maximum=10),
                    relationship_types=_relations(values),
                    max_depth=_integer(values, "max_depth", default=2, maximum=8),
                    max_nodes=_integer(values, "max_nodes", default=20, maximum=20),
                    direction=_direction(values, GraphDirection.BOTH),
                ),
            )
        )
        return {
            "ok": True,
            "seeds": list(response.seeds),
            "nodes": [compact_node(item) for item in response.nodes],
            "relations": [compact_relation(item) for item in response.relations],
            "diagnostics": response.diagnostics,
        }

    async def _paths(
        self,
        values: dict[str, object],
        access: AccessContext,
    ) -> dict[str, object]:
        response = await self.runtime.graph.find_paths(
            GraphPathRequest(
                access=access,
                query=GraphPathQuery(
                    start_node=_text(values, "start_node"),
                    end_node=_text(values, "end_node"),
                    relationship_types=_relations(values),
                    max_depth=_integer(values, "max_depth", default=4, maximum=8),
                    max_paths=_integer(values, "max_paths", default=10, maximum=20),
                    direction=_direction(values, GraphDirection.BOTH),
                ),
            )
        )
        return {
            "ok": True,
            "paths": [compact_path(item) for item in response.paths],
            "diagnostics": response.diagnostics,
        }

    async def _subgraph(
        self,
        values: dict[str, object],
        access: AccessContext,
    ) -> dict[str, object]:
        response = await self.runtime.graph.expand_subgraph(
            GraphSubgraphRequest(
                access=access,
                query=GraphSubgraphQuery(
                    start_node=_text(values, "start_node"),
                    relationship_types=_relations(values),
                    max_depth=_integer(values, "max_depth", default=2, maximum=8),
                    max_nodes=_integer(values, "max_nodes", default=20, maximum=20),
                    direction=_direction(values, GraphDirection.BOTH),
                ),
            )
        )
        return {
            "ok": True,
            "nodes": [compact_node(item) for item in response.nodes],
            "relations": [compact_relation(item) for item in response.relations],
            "diagnostics": response.diagnostics,
        }


def _access(values: dict[str, object], principal_id: str) -> AccessContext:
    return AccessContext(
        principal_id=principal_id,
        tenant_id=TenantId(_text(values, "tenant_id")),
    )


def _text(values: dict[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(values: dict[str, object], name: str) -> str | None:
    return _text(values, name) if values.get(name) is not None else None


def _integer(
    values: dict[str, object],
    name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    value = values.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _mapping(values: dict[str, object], name: str) -> dict[str, object]:
    value = values.get(name, {})
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    if "tenant_id" in value:
        raise ValueError("tenant_id is not allowed inside filters")
    return dict(value)


def _relations(values: dict[str, object]) -> tuple[RelationType, ...]:
    value = values.get("relationship_types", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("relationship_types must be an array of strings")
    return tuple(RelationType(item) for item in value)


def _direction(
    values: dict[str, object],
    default: GraphDirection,
) -> GraphDirection:
    return GraphDirection(_optional_text(values, "direction") or default.value)


__all__ = ["RuntimeAgentToolProvider"]
