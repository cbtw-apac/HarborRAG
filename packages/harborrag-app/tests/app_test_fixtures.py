"""Application-layer test doubles kept outside the production package."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from app_test_graph_records import (
    graph_payload,
    projection_inventory_payload,
    retrieval_payload,
)

from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.errors import IngestionAlreadyCompletedError
from harborrag_app.workflow_control.ingestion_models import IngestionCreateCommand
from harborrag_core.models.chat import HarborChatRequest
from harborrag_core.retrieval import GraphPathQuery, GraphSubgraphQuery, GraphTripletQuery
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.sdk import RetrievalLane


class MockAppService(BaseAppService):
    def __init__(self) -> None:
        self.submissions: list[IngestionCreateCommand] = []
        self.idempotency: dict[str, str] = {}
        self.retrieval_calls: list[dict[str, object]] = []
        self.graph_retrieval_calls: list[dict[str, object]] = []
        self.chat_calls: list[dict[str, object]] = []

    def health(self) -> AppResponse:
        return AppResponse(
            True,
            {
                "diagnostics": {
                    "mode": "development",
                    "runtime": {"provider": "app_test_double", "ready": True},
                }
            },
        )

    def ingest_once(self) -> AppResponse:
        return AppResponse(
            True,
            {
                "documents": ["mock://app/1"],
                "summary": {
                    "discovered": 1,
                    "loaded": 1,
                    "parsed": 1,
                    "indexed": 0,
                },
            },
        )

    async def chat_completion(
        self,
        request: HarborChatRequest,
        *,
        tenant_id: str,
        principal_id: str,
        prompt: ChatPrompt | None = None,
    ) -> AppResponse:
        self.chat_calls.append(
            {
                "request": request,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "prompt": prompt,
            }
        )
        return AppResponse(
            True,
            {
                "id": "chat-1",
                "created": 1_785_600_000,
                "model": request.logical_model or "primary",
                "provider": "mock",
                "provider_model": "mock-chat",
                "message": {"role": "assistant", "content": "Harbor response"},
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
                "latency_ms": 1.5,
                "retry_count": 0,
                "fallback_count": 0,
            },
        )

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        self.submissions.append(command)
        task_id = self.idempotency.get(idempotency_key or "")
        if task_id is None:
            task_id = str(uuid4())
            if idempotency_key is not None:
                self.idempotency[idempotency_key] = task_id
        return {
            "task_id": task_id,
            "status": "PENDING",
            "message": "Ingestion task accepted",
            "submitted_at": datetime(2026, 8, 1, 9, 24, tzinfo=UTC),
        }

    async def get_task(self, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "tenant": "DEFAULT",
            "status": "RUNNING",
            "stage": "PROCESSING_DOCUMENTS",
            "source": {"type": "local", "connection_id": "smoke-local"},
            "progress": {
                "discovered": 2,
                "admitted": 2,
                "processed": 1,
                "succeeded": 1,
                "failed": 0,
                "skipped": 0,
                "removed": 0,
            },
            "submitted_at": datetime(2026, 8, 1, 9, 24, tzinfo=UTC),
            "started_at": datetime(2026, 8, 1, 9, 24, 2, tzinfo=UTC),
            "completed_at": None,
            "message": "Processing admitted documents",
        }

    async def list_documents(
        self,
        *,
        task_id: str,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        del task_id, status, cursor, limit
        return {
            "items": [
                {
                    "document_id": "document:1",
                    "source_item_id": "adr/0001.md",
                    "document_kind": "file",
                    "title": "ADR-0001",
                    "status": "SUCCESS",
                    "active_document_version_id": "document-version:1",
                    "failure": None,
                    "updated_at": datetime(2026, 8, 1, 9, 25, tzinfo=UTC),
                }
            ],
            "next_cursor": None,
        }

    async def cancel(self, task_id: str) -> dict[str, object]:
        if task_id == "complete":
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        return {
            "task_id": task_id,
            "status": "RUNNING",
            "message": "Cancellation requested",
        }

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: list[str],
    ) -> dict[str, object]:
        return {
            "task_id": task_id,
            "retry_task_id": str(uuid4()),
            "accepted_document_count": len(document_ids) or 1,
            "message": "Failed documents accepted for retry",
        }

    async def start_ingestion(  # noqa: PLR0913 - mirrors the legacy CLI service port
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        connection_id: str | None = None,
        source_scope_id: str | None = None,
        path: str | None = None,
        pattern: str | None = None,
        recursive: bool = True,
        updated_after: str | None = None,
        max_artifacts: int | None = None,
        include_attachments: bool = True,
        filters: Mapping[str, object] | None = None,
        force_reprocess: bool = False,
        wait: bool = False,
    ) -> AppResponse:
        del (
            force_reprocess,
            include_attachments,
            max_artifacts,
            path,
            pattern,
            recursive,
            updated_after,
            wait,
        )
        return AppResponse(
            True,
            {
                "run": {
                    "run_id": run_id or "mock-run",
                    "tenant_id": tenant_id,
                    "connector_name": connector_name,
                    "connection_id": connection_id or connector_name,
                    "source_scope_id": source_scope_id or "mock-scope",
                    "filters": dict(filters or {}),
                },
                "workflow": {"workflow_id": "mock-workflow"},
            },
        )

    async def ingestion_status(self, run_id: str) -> AppResponse:
        return AppResponse(True, {"status": {"run_id": run_id, "status": "completed"}})

    async def ingestion_result(self, run_id: str) -> AppResponse:
        return AppResponse(True, {"result": {"run_id": run_id, "status": "completed"}})

    async def retrieve(  # noqa: PLR0913 - mirrors the application facade
        self,
        query: str,
        *,
        tenant_id: str | None = None,
        principal_id: str = "harborrag-cli",
        top_k: int = 10,
        filters: Mapping[str, object] | None = None,
        lane: RetrievalLane = RetrievalLane.HYBRID,
        observe_graph: bool = False,
        include_content: bool = False,
        include_metadata: bool = False,
        score_threshold: float = 0.0,
    ) -> AppResponse:
        self.retrieval_calls.append(
            {
                "query": query,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "top_k": top_k,
                "filters": dict(filters or {}),
                "lane": lane,
                "observe_graph": observe_graph,
                "include_content": include_content,
                "include_metadata": include_metadata,
                "score_threshold": score_threshold,
            }
        )
        return AppResponse(
            True,
            retrieval_payload(
                lane=lane,
                top_k=top_k,
                include_content=include_content,
                include_metadata=include_metadata,
                score_threshold=score_threshold,
            ),
        )

    async def retrieve_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._graph_response("triplets", query, tenant_id, principal_id)

    async def retrieve_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._graph_response("paths", query, tenant_id, principal_id)

    async def retrieve_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._graph_response("subgraph", query, tenant_id, principal_id)

    def _graph_response(
        self,
        operation: str,
        query: object,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        self.graph_retrieval_calls.append(
            {
                "operation": operation,
                "query": query,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
            }
        )
        return AppResponse(True, graph_payload(operation))

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
    ) -> AppResponse:
        return AppResponse(
            True,
            {"run_id": run_id, "action": action},
        )

    async def projection_inventory(self, tenant: str) -> dict[str, object]:
        return projection_inventory_payload(tenant)

    async def delete_projections(
        self,
        tenant: str,
        *,
        confirmation: str,
        stores: frozenset[str],
    ) -> dict[str, object]:
        del confirmation
        return {
            "tenant": tenant,
            "deleted_stores": sorted(stores),
            "before": await self.projection_inventory(tenant),
            "reindex_required": True,
        }
