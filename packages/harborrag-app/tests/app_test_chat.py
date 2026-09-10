"""Chat behavior for the shared application-service test double."""

from __future__ import annotations

from collections.abc import AsyncIterator

from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_core.contracts.errors import HarborNotFoundError, HarborValidationError


def require_allowed_model(
    model: str | None,
    *,
    tenant_id: str,
    allowed: set[str],
    tenant_models: dict[str, set[str]],
) -> None:
    """Reject a model this tenant may not use, exactly as the runtime does."""

    if model is None:
        return
    if model not in tenant_models.get(tenant_id, allowed):
        raise HarborValidationError(f"model {model!r} is not available", {"field": "model"})


class ChatServiceFixture:
    chat_calls: list[dict[str, object]]
    allowed_models: set[str]
    tenant_models: dict[str, set[str]]
    # The projects this double's control plane knows about. A ``project_id``
    # outside it is a 404 on the JSON and the streaming path alike, exactly as
    # ``require_project`` makes it in production.
    known_projects: set[str]

    async def validate_chat_project(self, project_id: str | None, *, tenant_id: str) -> None:
        del tenant_id
        if project_id is not None and project_id not in self.known_projects:
            raise HarborNotFoundError("Project was not found")

    async def validate_chat_model(self, model: str | None, *, tenant_id: str) -> None:
        require_allowed_model(
            model,
            tenant_id=tenant_id,
            allowed=self.allowed_models,
            tenant_models=self.tenant_models,
        )

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        exists = await self.chat_session_exists(
            options.session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        if not exists:
            raise HarborNotFoundError("Conversation session was not found")
        await self.validate_chat_project(options.project_id, tenant_id=tenant_id)
        self.chat_calls.append(self._chat_call(query, tenant_id, principal_id, options))
        return AppResponse(True, self._chat_payload(options.session_id, options.project_id))

    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]:
        self.chat_calls.append(self._chat_call(query, tenant_id, principal_id, options))
        return self._chat_stream_events(options.session_id)

    @staticmethod
    def _chat_call(
        query: str,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> dict[str, object]:
        return {
            "query": query,
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "system": options.system,
            "graph_search": options.graph_search,
            "session_id": options.session_id,
            "project_id": options.project_id,
            "user_id": options.user_id,
            "model": options.model,
        }

    async def _chat_stream_events(
        self,
        session_id: str,
    ) -> AsyncIterator[dict[str, object]]:
        yield {
            "kind": "citations",
            "citations": ({"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9},),
            "session_id": session_id,
        }
        yield {
            "kind": "chunk",
            "chunk": {
                "event": "text_delta",
                "model": "primary",
                "provider": "mock",
                "provider_model": "mock-chat",
                "content": "Harbor response",
                "reasoning": None,
                "finish_reason": None,
                "usage": None,
            },
        }
        yield {
            "kind": "chunk",
            "chunk": {
                "event": "completed",
                "model": "primary",
                "provider": "mock",
                "provider_model": "mock-chat",
                "content": None,
                "reasoning": None,
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        }

    @staticmethod
    def _chat_payload(session_id: str, project_id: str | None = None) -> dict[str, object]:
        return {
            "id": "chat-1",
            "created": 1_785_600_000,
            "model": "primary",
            "provider": "mock",
            "provider_model": "mock-chat",
            "message": {"role": "assistant", "content": "Harbor response"},
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            "latency_ms": 1.5,
            "retry_count": 0,
            "fallback_count": 0,
            "citations": [{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}],
            "session_id": session_id,
            "project_id": project_id,
        }


__all__ = ["ChatServiceFixture"]
