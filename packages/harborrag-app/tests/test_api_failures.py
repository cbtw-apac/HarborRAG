"""``GET /v1/admin/failures`` and ``POST /v1/admin/failures/retry`` under editor."""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app_test_connections import ConnectionWorld, connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_admin_connections import _BODY, _PATH, _SECRET, _token

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.ingestion.failure_inbox import FailureInboxService
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import (
    FailurePage,
    IngestionTask,
    IngestionTaskState,
    StoredFailure,
)

pytestmark = pytest.mark.unit

_LIST = "/v1/admin/failures"
_RETRY = "/v1/admin/failures/retry"
_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_ITEM_KEYS = {
    "failure_id",
    "ingestion_id",
    "connection_ref",
    "document_ref",
    "stage",
    "error_category",
    "message",
    "attempts",
    "retryable",
    "resolved",
    "created_at",
}


def _failure_id(task_id: str, document_id: str) -> str:
    raw = f"{task_id}\n{document_id}".encode()
    return "fail_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


class _Runs:
    """In-memory run registry with the failure inbox semantics of the real one."""

    def __init__(self) -> None:
        self.tasks: dict[str, IngestionTask] = {}
        self.failures: list[StoredFailure] = []
        self.selects: list[dict[str, Any]] = []

    def task(
        self, task_id: str, *, tenant: str, source_key: str, retry_of: str | None = None
    ) -> None:
        request: dict[str, Any] = {"tenant_id": tenant, "connection_id": source_key}
        if retry_of:
            request["retry_of"] = retry_of
        self.tasks[task_id] = IngestionTask(
            task_id=task_id,
            source_scope_id=f"scope-{source_key}",
            status=IngestionTaskState.FAILED,
            request=request,
            submitted_at=_T0,
        )

    def fail(  # noqa: PLR0913 - test builder
        self,
        task_id: str,
        document_id: str,
        *,
        source_key: str,
        minutes: int = 0,
        code: str | None = "parse_failed",
        stage: str | None = "ParseAndNormalize",
        retryable: bool = True,
        resolved: bool = False,
        attempts: int = 1,
    ) -> None:
        result: dict[str, object] = {
            "retryable": retryable,
            "source_item_id": f"item-{document_id}",
        }
        if code:
            result["safe_error_code"] = code
        if stage:
            result["failure_stage"] = stage
        self.failures.append(
            StoredFailure(
                task_id=task_id,
                document_id=document_id,
                source_key=source_key,
                result=result,
                failed_at=_T0 + timedelta(minutes=minutes),
                attempts=attempts,
                resolved=resolved,
            )
        )

    async def get(self, task_id: str) -> IngestionTask | None:
        return self.tasks.get(task_id)

    def _matching(  # noqa: PLR0913 - mirrors the registry filters
        self,
        *,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        known_stages: frozenset[str] | None,
        resolved: bool,
        q: str | None = None,
    ) -> list[StoredFailure]:
        def stage_ok(item: StoredFailure) -> bool:
            stage = item.result.get("failure_stage")
            if internal_stages is None or stage in internal_stages:
                return True
            return known_stages is not None and stage not in known_stages

        return sorted(
            (
                item
                for item in self.failures
                if item.resolved is resolved
                and (source_keys is None or item.source_key in source_keys)
                and (
                    error_category is None
                    or item.result.get("safe_error_code", "unknown") == error_category
                )
                and stage_ok(item)
                and (
                    q is None
                    or q.lower()
                    in f"{item.document_id} {item.result.get('source_item_id')}".lower()
                )
            ),
            key=lambda item: (item.failed_at, item.task_id, item.document_id),
            reverse=True,
        )

    async def failure_page(  # noqa: PLR0913 - mirrors the registry
        self,
        *,
        tenant_id: str,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        resolved: bool,
        q: str | None,
        before: tuple[datetime, str, str] | None,
        limit: int,
        include_total: bool,
        known_stages: frozenset[str] | None = None,
    ) -> FailurePage:
        found = self._matching(
            source_keys=source_keys,
            error_category=error_category,
            internal_stages=internal_stages,
            known_stages=known_stages,
            resolved=resolved,
            q=q,
        )
        counts: dict[str, int] = {}
        for item in found:
            code = str(item.result.get("safe_error_code", "unknown"))
            counts[code] = counts.get(code, 0) + 1
        if before is not None:
            found = [i for i in found if (i.failed_at, i.task_id, i.document_id) < before]
        return FailurePage(
            items=tuple(found[:limit]),
            has_more=len(found) > limit,
            counts=counts,
            total=sum(counts.values()) if include_total else None,
        )

    async def failures_for_retry(  # noqa: PLR0913 - mirrors the registry
        self,
        *,
        tenant_id: str,
        keys: Sequence[tuple[str, str]] | None,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        limit: int = 1000,
        known_stages: frozenset[str] | None = None,
    ) -> tuple[StoredFailure, ...]:
        self.selects.append({"internal_stages": internal_stages, "known_stages": known_stages})
        found = self._matching(
            source_keys=source_keys,
            error_category=error_category,
            internal_stages=internal_stages,
            known_stages=known_stages,
            resolved=False,
        )
        return tuple(
            item
            for item in found
            if item.result.get("retryable") is True
            and (keys is None or (item.task_id, item.document_id) in keys)
        )[:limit]


class _Actions:
    """Starts one retry run per call, as the application service does."""

    def __init__(self, runs: _Runs) -> None:
        self._runs = runs
        self.calls: list[dict[str, Any]] = []

    async def retry_failures(
        self, *, task_id: str, document_ids: Sequence[str], **kwargs: Any
    ) -> dict[str, object]:
        self.calls.append({"task_id": task_id, "document_ids": list(document_ids), **kwargs})
        original = self._runs.tasks[task_id]
        retry_id = f"retry-{len(self.calls)}"
        self._runs.task(
            retry_id,
            tenant=original.request["tenant_id"],
            source_key=original.request["connection_id"],
            retry_of=task_id,
        )
        return {
            "task_id": task_id,
            "retry_task_id": retry_id,
            "accepted_document_count": len(document_ids),
        }


class _App(MockAppService):
    def __init__(self, world: ConnectionWorld, runs: _Runs, actions: _Actions) -> None:
        super().__init__()
        self.connection_service = world.service
        self.failure_inbox = FailureInboxService(
            connections=lambda: world.control_plane.connections,
            runs=_provide(runs),
            actions=actions,  # type: ignore[arg-type]
        )


def _provide(runs: _Runs):  # type: ignore[no-untyped-def]
    async def provider() -> _Runs:
        return runs

    return provider


@pytest.fixture
def runs() -> _Runs:
    return _Runs()


@pytest.fixture
def actions(runs: _Runs) -> _Actions:
    return _Actions(runs)


@pytest.fixture
def keys() -> dict[str, str]:
    return {}


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, runs: _Runs, actions: _Actions, keys: dict[str, str]
) -> Iterator[TestClient]:
    world = connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])
    monkeypatch.setattr(api_app, "select_app_service", lambda: (_App(world, runs, actions), "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('editor', ['acme', 'globex'])}"
        test_client.headers["X-Tenant-Id"] = "acme"
        assert test_client.put(_PATH, json=_BODY).status_code == 201
        wiki = "/v1/admin/projects/cpm/connections/cpm-wiki"
        assert test_client.put(wiki, json=_BODY).status_code == 201
        keys.update(
            {
                c.connection_id: c.source_key
                for c in world.control_plane.connections.connections.values()
            }
        )
        yield test_client


@pytest.fixture
def seeded(runs: _Runs, keys: dict[str, str], client: TestClient) -> _Runs:
    tickets, wiki = keys["cpm-tickets"], keys["cpm-wiki"]
    runs.task("run-1", tenant="acme", source_key=tickets)
    runs.task("run-wiki", tenant="acme", source_key=wiki)
    for index in range(12):
        runs.fail("run-1", f"doc-parse-{index:02d}", source_key=tickets, minutes=index)
    for index in range(3):
        runs.fail(
            "run-1",
            f"doc-auth-{index}",
            source_key=tickets,
            minutes=20 + index,
            code="auth_expired",
            stage="FetchAndCaptureRaw",
        )
    runs.fail(
        "run-1", "doc-fatal", source_key=tickets, minutes=40, code="bad_input", retryable=False
    )
    runs.fail("run-1", "doc-done", source_key=tickets, minutes=41, resolved=True)
    runs.fail("run-wiki", "doc-wiki", source_key=wiki, minutes=50)
    return runs


def test_page_answers_the_failure_shape_with_counts_for_an_editor(
    client: TestClient, seeded: _Runs
) -> None:
    response = client.get(
        _LIST, params={"connection_ref": "cpm/cpm-tickets", "limit": 3, "include_total": "true"}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["counts"] == {"parse_failed": 12, "auth_expired": 3, "bad_input": 1}
    assert body["total"] == 16
    assert body["next_cursor"]
    first = body["items"][0]
    assert set(first) == _ITEM_KEYS
    assert first == {
        "failure_id": _failure_id("run-1", "doc-fatal"),
        "ingestion_id": "run-1",
        "connection_ref": "cpm/cpm-tickets",
        "document_ref": "item-doc-fatal",
        "stage": "interpret",
        "error_category": "bad_input",
        "message": "Document ingestion failed: bad_input",
        "attempts": 1,
        "retryable": False,
        "resolved": False,
        "created_at": (_T0 + timedelta(minutes=40)).isoformat().replace("+00:00", "Z"),
    }


def test_total_is_absent_unless_requested_and_cursor_walks_all_pages(
    client: TestClient, seeded: _Runs
) -> None:
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 5, **({"cursor": cursor} if cursor else {})}
        body = client.get(_LIST, params=params).json()
        assert body["total"] is None
        seen.extend(item["failure_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen)) == 17


def test_resolved_flag_lists_resolved_failures(client: TestClient, seeded: _Runs) -> None:
    body = client.get(_LIST, params={"resolved": "true"}).json()

    assert [item["document_ref"] for item in body["items"]] == ["item-doc-done"]
    assert body["items"][0]["resolved"] is True


def test_stage_capture_also_matches_missing_and_unknown_stages(
    client: TestClient, seeded: _Runs, keys: dict[str, str]
) -> None:
    wiki = keys["cpm-wiki"]
    seeded.fail("run-wiki", "doc-bare", source_key=wiki, minutes=60, stage=None)
    seeded.fail("run-wiki", "doc-odd", source_key=wiki, minutes=61, stage="Brand New")

    capture = client.get(_LIST, params={"stage": "capture", "limit": 100}).json()
    interpret = client.get(_LIST, params={"stage": "interpret", "limit": 100}).json()

    capture_refs = {item["document_ref"] for item in capture["items"]}
    assert {"item-doc-bare", "item-doc-odd", "item-doc-auth-0"} <= capture_refs
    assert "item-doc-parse-00" not in capture_refs
    assert {item["stage"] for item in capture["items"]} == {"capture"}
    assert "item-doc-bare" not in {item["document_ref"] for item in interpret["items"]}


def test_error_category_unknown_matches_failures_without_a_code(
    client: TestClient, seeded: _Runs, keys: dict[str, str]
) -> None:
    seeded.fail("run-wiki", "doc-bare", source_key=keys["cpm-wiki"], minutes=60, code=None)

    body = client.get(_LIST, params={"error_category": "unknown"}).json()

    assert [item["document_ref"] for item in body["items"]] == ["item-doc-bare"]
    assert body["items"][0]["error_category"] == "unknown"
    assert body["counts"] == {"unknown": 1}


def test_filters_narrow_by_project_connection_and_category(
    client: TestClient, seeded: _Runs
) -> None:
    wiki = client.get(_LIST, params={"connection_ref": "cpm/cpm-wiki"}).json()
    nowhere = client.get(_LIST, params={"project_id": "nope"}).json()
    auth = client.get(_LIST, params={"error_category": "auth_expired"}).json()
    text = client.get(_LIST, params={"q": "doc-wiki"}).json()

    assert [item["document_ref"] for item in wiki["items"]] == ["item-doc-wiki"]
    assert nowhere["items"] == []
    assert auth["counts"] == {"auth_expired": 3}
    assert [item["document_ref"] for item in text["items"]] == ["item-doc-wiki"]


def test_cursor_is_bound_to_its_filters_and_garbage_is_a_422(
    client: TestClient, seeded: _Runs
) -> None:
    cursor = client.get(_LIST, params={"limit": 2}).json()["next_cursor"]

    other = client.get(
        _LIST, params={"limit": 2, "cursor": cursor, "error_category": "parse_failed"}
    )
    garbage = client.get(_LIST, params={"cursor": "not-a-cursor"})

    assert other.status_code == 422
    assert other.json()["error"]["details"][0]["field"] == "cursor"
    assert garbage.status_code == 422


def test_reader_is_forbidden_on_both_routes(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {_token('reader', ['acme'])}"}

    assert client.get(_LIST, headers=headers).status_code == 403
    assert client.post(_RETRY, headers=headers, json={"filter": {}}).status_code == 403


def test_retry_by_filter_starts_one_run_of_the_root(
    client: TestClient, seeded: _Runs, actions: _Actions
) -> None:
    response = client.post(
        _RETRY,
        json={"filter": {"connection_ref": "cpm/cpm-tickets", "error_category": "parse_failed"}},
    )

    assert response.status_code == 202
    assert response.json() == {
        "ingestion_id": "retry-1",
        "ingestion_ids": ["retry-1"],
        "queued": 12,
    }
    assert len(actions.calls) == 1
    assert actions.calls[0]["task_id"] == "run-1"
    assert len(actions.calls[0]["document_ids"]) == 12
    assert actions.calls[0]["actor"] == "u_huy"
    assert seeded.tasks["retry-1"].request["retry_of"] == "run-1"


def test_retry_by_ids_queues_only_those_failures(
    client: TestClient, seeded: _Runs, actions: _Actions
) -> None:
    response = client.post(_RETRY, json={"ids": [_failure_id("run-1", "doc-parse-03")]})

    assert response.status_code == 202
    assert response.json() == {"ingestion_id": "retry-1", "ingestion_ids": ["retry-1"], "queued": 1}
    assert actions.calls[0]["document_ids"] == ["doc-parse-03"]


def test_retry_skips_resolved_and_not_retryable_failures(
    client: TestClient, seeded: _Runs, actions: _Actions
) -> None:
    response = client.post(
        _RETRY, json={"ids": [_failure_id("run-1", "doc-fatal"), _failure_id("run-1", "doc-done")]}
    )

    assert response.status_code == 202
    assert response.json() == {"ingestion_id": None, "ingestion_ids": [], "queued": 0}
    assert actions.calls == []


def test_retry_with_nothing_selected_starts_no_run(
    client: TestClient, seeded: _Runs, actions: _Actions
) -> None:
    response = client.post(_RETRY, json={"filter": {"error_category": "no_such_category"}})

    assert response.status_code == 202
    assert response.json() == {"ingestion_id": None, "ingestion_ids": [], "queued": 0}
    assert actions.calls == []
    assert set(seeded.tasks) == {"run-1", "run-wiki"}


@pytest.mark.parametrize(
    "body",
    [{}, {"ids": ["fail_x"], "filter": {}}, {"ids": []}, {"filter": {"stage": "nope"}}],
)
def test_retry_needs_exactly_one_of_ids_or_filter(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post(_RETRY, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_an_undecodable_failure_id_is_a_422_on_ids(client: TestClient) -> None:
    response = client.post(_RETRY, json={"ids": ["fail_!!!", "not-prefixed"]})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "ids"


def test_failures_of_a_retry_run_are_retried_through_the_root(
    client: TestClient, seeded: _Runs, actions: _Actions, keys: dict[str, str]
) -> None:
    tickets = keys["cpm-tickets"]
    seeded.task("retry-old", tenant="acme", source_key=tickets, retry_of="run-1")
    seeded.task("retry-older", tenant="acme", source_key=tickets, retry_of="retry-old")
    seeded.fail(
        "retry-older", "doc-again", source_key=tickets, minutes=70, code="again", attempts=2
    )

    response = client.post(_RETRY, json={"filter": {"error_category": "again"}})

    assert response.json()["queued"] == 1
    assert actions.calls[0]["task_id"] == "run-1"
    assert actions.calls[0]["document_ids"] == ["doc-again"]


def test_two_roots_start_two_runs_and_queued_is_summed(
    client: TestClient, seeded: _Runs, actions: _Actions
) -> None:
    response = client.post(_RETRY, json={"filter": {"error_category": "parse_failed"}})

    assert response.status_code == 202
    body = response.json()
    assert body["ingestion_ids"] == ["retry-1", "retry-2"]
    assert body["ingestion_id"] == "retry-1"
    assert body["queued"] == 13
    assert {call["task_id"] for call in actions.calls} == {"run-1", "run-wiki"}


def test_retry_stage_capture_filter_passes_the_known_stages(
    client: TestClient, seeded: _Runs
) -> None:
    client.post(_RETRY, json={"filter": {"stage": "capture"}})
    client.post(_RETRY, json={"filter": {"stage": "interpret"}})

    capture, interpret = seeded.selects
    assert capture["internal_stages"] == {"FetchAndCaptureRaw"}
    assert capture["known_stages"] and "ParseAndNormalize" in capture["known_stages"]
    assert interpret["known_stages"] is None
    assert interpret["internal_stages"] == {"ParseAndNormalize", "ChunkAndValidate"}
