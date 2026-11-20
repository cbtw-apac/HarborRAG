from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_admin_connections import _BODY, _PATH, _SECRET, _token
from test_api_failures import _Actions
from test_api_failures import _App as _FailuresApp
from test_api_failures import _Runs as _FailureRuns
from test_ingestion_documents_page import _App as _DocumentsApp
from test_ingestion_documents_page import _Runs as _DocumentRuns

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.operations.schemas.ingestions import (
    FailureOut,
    FailureQuery,
    IngestionDocumentItem,
)
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import StoredFailure
from harborrag_runtime.testing.connection_fakes import connection_world

pytestmark = pytest.mark.unit

_SOURCE_ITEM_IDS = ["docs/notes.md ", " leading.md"]
_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_PROJECTS = [Project(id="demo", name="DEMO", collection="demo", tenant_id="acme")]


@contextmanager
def _editor_client(
    monkeypatch: pytest.MonkeyPatch, service: MockAppService
) -> Iterator[TestClient]:
    """Serve ``service`` to an acme editor through the real FastAPI app."""
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {_token('editor', ['acme'])}"
        client.headers["X-Tenant-Id"] = "acme"
        yield client


@pytest.mark.parametrize("source_item_id", _SOURCE_ITEM_IDS)
def test_ingestion_document_item_with_edge_whitespace_keeps_document_ref(
    source_item_id: str,
) -> None:
    """A document item answers a source item ID with its edge spaces kept."""
    item = IngestionDocumentItem.model_validate(
        {
            "document_ref": source_item_id,
            "document_id": "doc-1",
            "status": "succeeded",
            "stage": "publish",
        }
    )

    assert item.model_dump(mode="json")["document_ref"] == source_item_id


@pytest.mark.parametrize("source_item_id", _SOURCE_ITEM_IDS)
def test_failure_out_with_edge_whitespace_keeps_document_ref(source_item_id: str) -> None:
    """A failure answers a source item ID with its edge spaces kept."""
    failure = FailureOut.model_validate(
        {
            "failure_id": "fail_1",
            "ingestion_id": "run-1",
            "connection_ref": "demo/demo-tickets",
            "document_ref": source_item_id,
            "stage": "interpret",
            "error_category": "parse_failed",
            "message": "Document ingestion failed: parse_failed",
            "attempts": 1,
            "retryable": True,
            "resolved": False,
            "created_at": _T0,
        }
    )

    assert failure.model_dump(mode="json")["document_ref"] == source_item_id


@pytest.mark.parametrize("source_item_id", _SOURCE_ITEM_IDS)
def test_documents_page_with_edge_whitespace_answers_the_source_item_id(
    monkeypatch: pytest.MonkeyPatch, source_item_id: str
) -> None:
    """The documents page answers the stored source item ID unchanged."""
    runs = _DocumentRuns()
    runs.add("doc-1", "published", 0, source_item_id=source_item_id)
    service = _DocumentsApp(connection_world(_PROJECTS), runs)

    with _editor_client(monkeypatch, service) as client:
        response = client.get("/v1/admin/ingestions/ing_1/documents")

    assert response.status_code == 200
    assert [item["document_ref"] for item in response.json()["items"]] == [source_item_id]


@pytest.mark.parametrize("source_item_id", _SOURCE_ITEM_IDS)
def test_failures_page_with_edge_whitespace_answers_the_source_item_id(
    monkeypatch: pytest.MonkeyPatch, source_item_id: str
) -> None:
    """The failure inbox answers the stored source item ID unchanged."""
    world = connection_world(_PROJECTS)
    runs = _FailureRuns()

    with _editor_client(monkeypatch, _FailuresApp(world, runs, _Actions(runs))) as client:
        assert client.put(_PATH, json=_BODY).status_code == 201
        (connection,) = world.control_plane.connections.connections.values()
        runs.task("run-1", tenant="acme", source_key=connection.source_key)
        runs.failures.append(
            StoredFailure(
                task_id="run-1",
                document_id="doc-1",
                source_key=connection.source_key,
                result={"source_item_id": source_item_id, "safe_error_code": "parse_failed"},
                failed_at=_T0,
                attempts=1,
                resolved=False,
            )
        )
        response = client.get("/v1/admin/failures")

    assert response.status_code == 200
    assert [item["document_ref"] for item in response.json()["items"]] == [source_item_id]


def test_failure_query_with_edge_whitespace_still_trims_request_text() -> None:
    """Request models keep trimming surrounding whitespace."""
    assert FailureQuery(q="  docs/notes.md ").q == "docs/notes.md"
