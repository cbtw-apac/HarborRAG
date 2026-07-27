from __future__ import annotations

from harborrag_core.ports.indexing import GraphIndexRepositoryPort
from harborrag_core.schemas.graph import GraphEdge, GraphNode

from ..errors import GraphIndexValidationError
from ..schemas import ChunkDiffResult, IndexingRequest
from .planner import GraphMutationPlanner
from .schemas import GraphIndexResult, GraphMutationPlan
from .validation import GraphValidationService


class GraphIndexService:
    """Upsert a deterministic graph generation and validate exact identities."""

    def __init__(
        self,
        *,
        graph_repository: GraphIndexRepositoryPort,
        planner: GraphMutationPlanner | None = None,
        validator: GraphValidationService | None = None,
    ) -> None:
        """Initialize graph repository, planner, and validation boundaries."""

        self._graph_repository = graph_repository
        self._planner = planner or GraphMutationPlanner()
        self._validator = validator or GraphValidationService()

    def plan(
        self,
        request: IndexingRequest,
        diff: ChunkDiffResult | None = None,
    ) -> GraphMutationPlan:
        """Build the deterministic graph plan without provider calls."""

        return self._planner.plan(request, diff)

    async def stage(
        self,
        request: IndexingRequest,
        plan: GraphMutationPlan | None = None,
    ) -> GraphIndexResult:
        """Persist and validate a staged graph mutation plan."""

        plan = plan or self.plan(request)
        if plan.nodes:
            await self._graph_repository.upsert_nodes(
                plan.nodes,
                context=request.context,
            )
        if plan.edges:
            await self._graph_repository.upsert_edges(
                plan.edges,
                context=request.context,
            )
        nodes: tuple[GraphNode, ...] = tuple(
            await self._graph_repository.get_nodes(
                [node.id for node in plan.nodes],
                context=request.context,
            )
        )
        edges: tuple[GraphEdge, ...] = tuple(
            await self._graph_repository.get_edges(
                [edge.id for edge in plan.edges],
                context=request.context,
            )
        )
        validation = self._validator.validate(plan, nodes, edges)
        if not validation.valid:
            raise GraphIndexValidationError("; ".join(validation.errors))
        return GraphIndexResult(plan=plan, validation=validation)
