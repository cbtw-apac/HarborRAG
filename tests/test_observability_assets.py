"""The shipped dashboards and alert rules keep the contracts the runbooks and links rely on."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from harborrag_core.observability.log_events import SAFE_FIELD_NAMES, SAFE_FIELD_SUFFIXES

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = sorted((ROOT / "deploy/grafana/dashboards").glob("*/*.json"))
INGESTION_DASHBOARD = ROOT / "deploy/grafana/dashboards/offline/ingestion-schedules.json"
DATA_SOURCE_UIDS = {
    "harborrag-prometheus",
    "harborrag-loki",
    "harborrag-tempo",
    "-- Grafana --",
}
RUN_STATUSES = {"pending", "running", "paused", "completed", "partial", "failed", "cancelled"}
RUNBOOK = ROOT / "docs/developers/deployment/observability-runbooks.md"


def _nodes_under(key: str, node: object) -> list[Any]:
    """Return every value stored under ``key`` anywhere in a parsed JSON document."""
    found: list[Any] = []
    if isinstance(node, dict):
        if key in node:
            found.append(node[key])
        for value in node.values():
            found.extend(_nodes_under(key, value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_nodes_under(key, value))
    return found


def _data_sources(board: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every data source reference of a dashboard's panels, queries and annotations."""
    return [source for source in _nodes_under("datasource", board) if isinstance(source, dict)]


def _status_codes(expr: str) -> dict[str, str]:
    """Return the number a query gives each run status, keyed by the number as Grafana maps it."""
    return {code: status for status, code in re.findall(r'status="(\w+)"\} \* (\d+)', expr)}


def _anchors(markdown: str) -> set[str]:
    """Return the anchors the docs site gives the page's headings."""
    headings = re.findall(r"^#+ (.+)$", markdown, flags=re.MULTILINE)
    return {re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-") for heading in headings}


def test_the_five_dashboards_ship_with_fixed_uids_in_their_folders() -> None:
    boards = {
        path.relative_to(ROOT / "deploy/grafana/dashboards").as_posix(): path for path in DASHBOARDS
    }

    uids = {
        name: json.loads(path.read_text(encoding="utf-8"))["uid"] for name, path in boards.items()
    }

    assert uids == {
        "offline/ingestion-schedules.json": "harborrag-ingestion",
        "online/chat-llm.json": "harborrag-chat-llm",
        "online/mcp-retrieval.json": "harborrag-mcp-retrieval",
        "online/online-overview.json": "harborrag-online-overview",
        "shared/stores-host.json": "harborrag-stores-host",
    }


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda path: path.stem)
def test_dashboards_use_only_the_provisioned_data_sources(path: Path) -> None:
    board = json.loads(path.read_text(encoding="utf-8"))

    assert {source["uid"] for source in _data_sources(board)} <= DATA_SOURCE_UIDS
    assert {variable["name"] for variable in board["templating"]["list"]} >= {"env", "tenant"}


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda path: path.stem)
def test_no_dashboard_queries_a_sql_data_source(path: Path) -> None:
    board = json.loads(path.read_text(encoding="utf-8"))

    types = {source.get("type", "") for source in _data_sources(board)}

    assert [kind for kind in types if "sql" in kind or "postgres" in kind] == []
    assert _nodes_under("rawSql", board) == []


def test_run_status_codes_map_back_to_the_status_each_one_encodes() -> None:
    board = json.loads(INGESTION_DASHBOARD.read_text(encoding="utf-8"))
    checked = []

    for panel in board["panels"]:
        overrides = [
            item["value"]
            for properties in _nodes_under("properties", panel)
            for item in properties
            if item["id"] == "mappings"
        ]
        named_codes = [
            {code: option["text"].lower() for code, option in mapping["options"].items()}
            for mappings in [*_nodes_under("mappings", panel), *overrides]
            for mapping in mappings
            if mapping["type"] == "value"
        ]
        for expr in _nodes_under("expr", panel):
            codes = _status_codes(expr)
            if codes:
                assert codes in named_codes, panel["title"]
                assert set(codes.values()) == RUN_STATUSES, panel["title"]
                checked.append(panel["title"])

    assert sorted(checked) == ["Last sync per source and tenant", "Source runs"]


def test_postgres_exporter_logs_in_as_the_monitor_role_without_deprecated_variables() -> None:
    compose = yaml.safe_load(
        (ROOT / "deploy/compose/docker-compose.monitoring.yml").read_text(encoding="utf-8")
    )
    exporter = compose["services"]["postgres-exporter"]

    assert exporter["environment"] == {"DATA_SOURCE_USER": "harborrag_monitor"}
    assert exporter["command"][0].startswith("--datasource.uri=postgres:5432/${POSTGRES_DB:?")
    assert exporter["command"][1] == "--datasource.pass-file=/run/secrets/postgres-monitor-password"
    assert exporter["secrets"] == ["postgres-monitor-password"]
    assert compose["secrets"]["postgres-monitor-password"] == {
        "environment": "HARBORRAG_MONITOR_PASSWORD"
    }


def test_every_alert_is_routed_and_links_an_existing_runbook_section() -> None:
    rules = yaml.safe_load(
        (ROOT / "deploy/prometheus/harborrag_alerts.yml").read_text(encoding="utf-8")
    )
    alerts = [rule for group in rules["groups"] for rule in group["rules"] if "alert" in rule]
    anchors = _anchors(RUNBOOK.read_text(encoding="utf-8"))
    dashboard_uids = {json.loads(path.read_text(encoding="utf-8"))["uid"] for path in DASHBOARDS}

    for alert in alerts:
        assert alert["labels"]["severity"] in {"critical", "warning"}, alert["alert"]
        assert alert["labels"]["side"] in {"online", "offline", "shared"}, alert["alert"]
        assert alert["annotations"]["dashboard"] in dashboard_uids, alert["alert"]
        runbook = alert["annotations"].get("runbook_url")
        if runbook is not None:
            assert runbook.split("#", 1)[1] in anchors, alert["alert"]
    assert len({alert["alert"] for alert in alerts}) == 15


def test_the_collector_keeps_every_field_the_log_helper_allows() -> None:
    config = yaml.safe_load(
        (ROOT / "deploy/otel-collector/config.yaml").read_text(encoding="utf-8")
    )
    statements = config["processors"]["transform/allowlist"]["log_statements"][0]["statements"]
    keep = next(item for item in statements if item.startswith("keep_matching_keys(log.attributes"))
    match = re.search(r'"(.*)"\)$', keep)
    assert match is not None
    allowed = re.compile(match.group(1).replace("\\\\", "\\"))
    names = [*SAFE_FIELD_NAMES, *(f"sample{suffix}" for suffix in SAFE_FIELD_SUFFIXES)]

    assert [name for name in names if not allowed.fullmatch(name)] == []
