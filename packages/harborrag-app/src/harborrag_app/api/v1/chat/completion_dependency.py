"""The chat application facade used by the completion endpoint."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_core.ports.completion_requests import CompletionRequestStore

from .dependencies import ChatService


class CompletionService(ChatService, CompletionRequestStore, Protocol):
    """Compose chat execution and durable replay without duplicating their contracts."""


def completion_service(request: Request) -> CompletionService:
    return cast(CompletionService, request.app.state.app_service)


CompletionServiceDependency = Annotated[CompletionService, Depends(completion_service)]
