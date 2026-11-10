"""The role, response model and serialization flags of every ``/v1`` operation, declared once.

The HTTP routes read their decorator arguments and role guards from ``OPERATIONS``; the CLI
reads the same entries to enforce the role and to print the body the route would return.
Nothing here imports a web framework; the response models live in schema modules that do not
either.

Start from: OPERATIONS, route_kwargs, dump_body.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import TypeAdapter

from harborrag_app.api.v1.admin.connection_schemas import (
    ConnectionChipPage,
    ConnectionOut,
    ConnectionPage,
    ConnectionTestOut,
    IngestionOut,
    JobAcceptedOut,
    ProjectOut,
    ProjectPage,
    SourceTypePage,
)
from harborrag_app.api.v1.admin.settings_schemas import SettingsOut, SettingsPatchOut
from harborrag_app.api.v1.chat.schemas import (
    ChatCompletionResponse,
    MessageList,
    Session,
    SessionList,
)
from harborrag_app.api.v1.ingestion.schemas import (
    FailurePage,
    FailureRetryAccepted,
    IngestionDocumentPage,
    IngestionPage,
)
from harborrag_app.api.v1.me_schemas import MeOut
from harborrag_core.domain.member import Role


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """How one operation is guarded and serialized.

    Args:
        role: Lowest role that may call it.
        response_model: Model or type of the success body; ``None`` when it has none or streams.
        status_code: Status of the success response.
        by_alias: Whether the body is serialized with field aliases.
        exclude_unset: Whether fields the handler did not set are left out of the body.
    """

    role: Role
    response_model: Any = None
    status_code: int = 200
    by_alias: bool = True
    exclude_unset: bool = False


def _spec(role: Role, response_model: Any = None, **flags: Any) -> OperationSpec:
    """Build an entry; ``flags`` are the non-default ``OperationSpec`` fields."""
    return OperationSpec(role, response_model, **flags)


OPERATIONS: Mapping[str, OperationSpec] = {
    "me": _spec("reader", MeOut),
    "list_source_types": _spec("editor", SourceTypePage),
    "list_connections": _spec("editor", ConnectionPage),
    "get_connection": _spec("editor", ConnectionOut),
    "put_connection": _spec("editor", ConnectionOut),
    "delete_connection": _spec("editor", JobAcceptedOut, status_code=202),
    "test_connection": _spec("editor", ConnectionTestOut),
    "list_projects": _spec("admin", ProjectPage),
    "get_project": _spec("admin", ProjectOut),
    "put_project": _spec("admin", ProjectOut),
    "delete_project": _spec("admin", status_code=204),
    "list_searchable_connections": _spec("reader", ConnectionChipPage),
    "start_ingestion": _spec("editor", IngestionOut, status_code=202),
    "list_ingestions": _spec("editor", IngestionPage),
    "get_ingestion": _spec("editor", IngestionOut),
    "list_ingestion_documents": _spec("editor", IngestionDocumentPage),
    "stream_ingestion": _spec("editor"),
    "pause_ingestion": _spec("editor", IngestionOut),
    "resume_ingestion": _spec("editor", IngestionOut),
    "cancel_ingestion": _spec("editor", IngestionOut),
    "retry_ingestion_failures": _spec("editor", IngestionOut, status_code=202),
    "list_failures": _spec("editor", FailurePage, exclude_unset=True),
    "retry_failures": _spec("editor", FailureRetryAccepted, status_code=202),
    "read_settings": _spec("editor", SettingsOut),
    "patch_settings": _spec("admin", SettingsPatchOut),
    "settings_schema": _spec("editor", dict[str, Any]),
    "create_chat_completion": _spec("reader", ChatCompletionResponse),
    "create_chat_session": _spec("reader", Session, status_code=201),
    "list_chat_sessions": _spec("reader", SessionList),
    "list_conversation_messages": _spec("reader", MessageList),
}


def route_kwargs(operation_id: str) -> dict[str, Any]:
    """Return the route decorator arguments that follow from an operation's entry.

    Args:
        operation_id: Key of ``OPERATIONS``, the route function's name.

    Returns:
        ``response_model``, ``status_code`` and the two ``response_model_*`` flags.
    """
    spec = OPERATIONS[operation_id]
    return {
        "response_model": spec.response_model,
        "status_code": spec.status_code,
        "response_model_by_alias": spec.by_alias,
        "response_model_exclude_unset": spec.exclude_unset,
    }


@cache
def _adapter(operation_id: str) -> TypeAdapter[Any]:
    """Validate and dump against the entry's response model."""
    return TypeAdapter(OPERATIONS[operation_id].response_model)


def dump_body(operation_id: str, body: object) -> object:
    """Serialize ``body`` the way the operation's route serializes its response.

    Args:
        operation_id: Key of ``OPERATIONS``.
        body: What the operation returned: a model, a dict or a list.

    Returns:
        JSON-ready data validated into the response model, with the entry's flags applied;
        ``body`` unchanged when the operation has no response model.
    """
    spec = OPERATIONS[operation_id]
    if spec.response_model is None:
        return body
    adapter = _adapter(operation_id)
    return adapter.dump_python(
        adapter.validate_python(body),
        mode="json",
        by_alias=spec.by_alias,
        exclude_unset=spec.exclude_unset,
    )
