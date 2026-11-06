"""``GET /v1/admin/ingestions/{ingestion_id}/documents`` and ``/events`` under editor."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from app_test_connections import ConnectionWorld, connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_admin_connections import _SECRET, _token

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.connection_views import IngestionLinks
from harborrag_app.workflow_control.ingestion.operations import IngestionOperations
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import (
    IngestionTask,
    IngestionTaskState,
    StoredTaskDocumentResult,
    TaskDocumentPage,
)

_URL = "/v1/admin/ingestions/ing_1/documents"
_ITEM_KEYS = {"document_ref", "document_id", "status", "stage", "error_category", "duration_ms"}
_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


class _Runs:
    """Run registry double holding one run per tenant and its stored document results."""

    def __init__(self) -> None:
        self.tasks = {
            "ing_1": self._task("ing_1", "acme"),
            "ing_other": self._task("ing_other", "globex"),
        }
        self.results: list[StoredTaskDocumentResult] = []

    @staticmethod
    def _task(task_id: str, tenant: str) -> IngestionTask:
        return IngestionTask(
            task_id=task_id,
            source_scope_id="scope",
            status=IngestionTaskState.COMPLETED,
            request={"tenant_id": tenant, "connection_id": "x/y"},
            submitted_at=_T0,
        )

    def add(self, document_id: str, status: str, age: int, **result: object) -> None:
        self.results.append(
            StoredTaskDocumentResult(
                task_id="ing_1",
                document_id=document_id,
                document_version_id=None,
                status=status,
                result=dict(result),
                updated_at=_T0 + timedelta(minutes=age),
            )
        )

    async def get(self, task_id: str) -> IngestionTask | None:
        return self.tasks.get(task_id)

    async def progress(self, task_id: str) -> dict[str, int]:
        return {}

    async def document_results_page(  # noqa: PLR0913 - mirrors the registry
        self,
        task_id: str,
        *,
        statuses: Sequence[str] | None = None,
        after_updated_at: datetime | None = None,
        after_document_id: str | None = None,
        limit: int = 50,
    ) -> TaskDocumentPage:
        found = sorted(
            (
                result
                for result in self.results
                if result.task_id == task_id
                and (statuses is None or result.status in statuses)
                and (
                    after_updated_at is None
                    or (result.updated_at, result.document_id)
                    > (after_updated_at, after_document_id)
                )
            ),
            key=lambda result: (result.updated_at, result.document_id),
        )
        return TaskDocumentPage(tuple(found[:limit]), has_more=len(found) > limit)


class _App(MockAppService):
    def __init__(self, world: ConnectionWorld, runs: _Runs) -> None:
        super().__init__()

        async def provider() -> _Runs:
            return runs

        self.ingestion_operations = IngestionOperations(
            connections=lambda: world.control_plane.connections,
            runs=provider,  # type: ignore[arg-type]
            links=IngestionLinks(None, "default", None),
            actions=world.submitter,  # type: ignore[arg-type]
        )


@pytest.fixture
def runs() -> _Runs:
    stored = _Runs()
    stored.add("doc-a", "published", 0, source_item_id="wiki/a.md")
    stored.add("doc-b", "unchanged", 1)
    stored.add(
        "doc-c",
        "failed",
        2,
        failure_stage="ParseAndNormalize",
        safe_error_code="parse_failed",
        title="Alex Example CV",
    )
    stored.add("doc-d", "removed", 3)
    stored.add("doc-e", "cancelled", 4)
    return stored


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, runs: _Runs) -> Iterator[TestClient]:
    world = connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])
    monkeypatch.setattr(api_app, "select_app_service", lambda: (_App(world, runs), "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('editor', ['acme', 'globex'])}"
        test_client.headers["X-Tenant-Id"] = "acme"
        yield test_client


def test_page_lists_succeeded_skipped_and_failed_in_the_explorer_shape(client: TestClient) -> None:
    response = client.get(_URL)

    assert response.status_code == 200
    items = response.json()["items"]
    assert all(set(item) == _ITEM_KEYS for item in items)
    assert items == [
        {
            "document_ref": "wiki/a.md",
            "document_id": "doc-a",
            "status": "succeeded",
            "stage": "publish",
            "error_category": None,
            "duration_ms": None,
        },
        {
            "document_ref": "doc-b",
            "document_id": "doc-b",
            "status": "skipped",
            "stage": "publish",
            "error_category": None,
            "duration_ms": None,
        },
        {
            "document_ref": "doc-c",
            "document_id": "doc-c",
            "status": "failed",
            "stage": "interpret",
            "error_category": "parse_failed",
            "duration_ms": None,
        },
    ]
    assert response.json()["next_cursor"] is None


def test_status_filter_keeps_one_status(client: TestClient) -> None:
    failed = client.get(_URL, params={"status": "failed"}).json()["items"]
    succeeded = client.get(_URL, params={"status": "succeeded"}).json()["items"]
    skipped = client.get(_URL, params={"status": "skipped"}).json()["items"]

    assert [item["document_id"] for item in failed] == ["doc-c"]
    assert [item["document_id"] for item in succeeded] == ["doc-a"]
    assert [item["document_id"] for item in skipped] == ["doc-b"]


def test_status_outside_the_enum_is_a_422_on_status(client: TestClient) -> None:
    response = client.get(_URL, params={"status": "removed"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "status"


def test_cursor_walks_the_pages(client: TestClient) -> None:
    first = client.get(_URL, params={"limit": 2}).json()
    second = client.get(_URL, params={"limit": 2, "cursor": first["next_cursor"]}).json()

    assert [item["document_id"] for item in first["items"]] == ["doc-a", "doc-b"]
    assert [item["document_id"] for item in second["items"]] == ["doc-c"]
    assert second["next_cursor"] is None


def test_limit_is_between_1_and_100(client: TestClient) -> None:
    assert client.get(_URL, params={"limit": 100}).status_code == 200
    assert client.get(_URL, params={"limit": 101}).status_code == 422


def test_a_run_of_another_tenant_or_a_missing_run_is_404(client: TestClient) -> None:
    assert client.get("/v1/admin/ingestions/ing_other/documents").status_code == 404
    assert client.get("/v1/admin/ingestions/missing/documents").status_code == 404
    assert client.get("/v1/admin/ingestions/ing_other/events").status_code == 404


def test_reader_is_forbidden_on_documents_and_events(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {_token('reader', ['acme'])}"}

    assert client.get(_URL, headers=headers).status_code == 403
    assert client.get("/v1/admin/ingestions/ing_1/events", headers=headers).status_code == 403
