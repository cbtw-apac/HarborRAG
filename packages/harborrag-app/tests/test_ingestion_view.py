"""``ingestion_view`` fills the spec's ``Ingestion`` object from a task, progress and links."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from harborrag_app.workflow_control.control_plane.connection_views import (
    IngestionLinks,
    ingestion_view,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState


def _task(
    request: dict[str, object] | None = None, summary: dict[str, object] | None = None
) -> IngestionTask:
    return IngestionTask(
        task_id="run-1",
        source_scope_id="scope",
        status=IngestionTaskState.RUNNING,
        request={"tenant_id": "acme", "connection_id": "default/jira-main"} | (request or {}),
        summary=summary or {},
        submitted_at=datetime(2026, 11, 6, tzinfo=UTC),
    )


def _view(task: IngestionTask, **kwargs: Any) -> dict[str, Any]:
    return ingestion_view(task, connection=None, **kwargs)


def test_counters_count_published_only() -> None:
    view = _view(
        _task(summary={"discovered": 10}),
        progress={"published": 5, "unchanged": 3, "failed": 2},
    )

    assert view["counters"] == {"discovered": 10, "processed": 5, "skipped": 3, "failed": 2}


def test_links_use_configured_bases() -> None:
    links = IngestionLinks(
        temporal_ui_url="http://t:8233", temporal_namespace="harborrag", grafana_url="http://g:3000"
    )

    view = _view(_task(), links=links)

    assert "harborrag-source:run-1" in view["links"]["temporal"]
    assert "run-1" in view["links"]["grafana"]
    assert view["links"]["langfuse"] == ""
    assert _view(_task())["links"] == {"temporal": "", "grafana": "", "langfuse": ""}
    unset = IngestionLinks(temporal_ui_url=None, temporal_namespace="harborrag", grafana_url=None)
    assert _view(_task(), links=unset)["links"] == {"temporal": "", "grafana": "", "langfuse": ""}


def test_links_drop_a_trailing_slash_of_the_bases() -> None:
    links = IngestionLinks(
        temporal_ui_url="http://x:8080/",
        temporal_namespace="harborrag",
        grafana_url="http://g:3000/",
    )

    view = _view(_task(), links=links)

    assert view["links"]["temporal"] == (
        "http://x:8080/namespaces/harborrag/workflows/harborrag-source:run-1"
    )
    assert view["links"]["grafana"] == "http://g:3000/d/harborrag-ingestion?var-run=run-1"


def test_retry_run_workflow_id() -> None:
    view = _view(_task({"retry_of": "run-0", "document_ids": ["a", "b", "c"]}))

    assert view["workflow_id"] == "harborrag-retry:run-1"
    assert view["counters"]["discovered"] == 3


def test_principal_type_from_request() -> None:
    view = _view(_task({"actor_type": "api_key", "actor": "key-1"}))

    assert view["triggered_by"] == {"principal_type": "api_key", "principal_id": "key-1"}
    assert _view(_task())["triggered_by"]["principal_type"] == "user"


def test_run_of_deleted_connection_falls_back_to_source_key() -> None:
    view = _view(_task())
    bare = _view(_task({"connection_id": "jira-main"}))

    assert (view["tenant_id"], view["project_id"], view["connection_id"]) == (
        "acme",
        "default",
        "jira-main",
    )
    assert (bare["project_id"], bare["connection_id"]) == ("", "jira-main")


def test_discovered_never_below_counted_results() -> None:
    view = _view(_task(), progress={"published": 2, "failed": 1})

    assert view["counters"] == {"discovered": 3, "processed": 2, "skipped": 0, "failed": 1}


def test_settings_revision_prefers_settings_revision() -> None:
    both = _task({"settings_revision": "rev_7", "configuration_fingerprint": "abc"})

    assert _view(both)["settings_revision"] == "rev_7"
    assert _view(_task({"configuration_fingerprint": "abc"}))["settings_revision"] == "abc"
    assert _view(_task())["settings_revision"] == ""
