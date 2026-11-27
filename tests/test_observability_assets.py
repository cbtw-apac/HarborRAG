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
DATA_SOURCE_UIDS = {
    "harborrag-prometheus",
    "harborrag-loki",
    "harborrag-tempo",
    "harborrag-postgres",
    "-- Grafana --",
}
RUNBOOK = ROOT / "docs/developers/deployment/observability-runbooks.md"


def _data_source_uids(node: object) -> set[str]:
    """Return every data source uid a dashboard refers to."""
    found: set[str] = set()
    if isinstance(node, dict):
        source = node.get("datasource")
        if isinstance(source, dict) and isinstance(source.get("uid"), str):
            found.add(source["uid"])
        for value in node.values():
            found |= _data_source_uids(value)
    elif isinstance(node, list):
        for value in node:
            found |= _data_source_uids(value)
    return found


def _sql(board: dict[str, Any]) -> list[str]:
    """Return the SQL of every PostgreSQL panel and annotation."""
    targets = [target for panel in board["panels"] for target in panel.get("targets", [])]
    return [
        item["rawSql"] for item in [*targets, *board["annotations"]["list"]] if "rawSql" in item
    ]


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

    assert _data_source_uids(board) <= DATA_SOURCE_UIDS
    assert {variable["name"] for variable in board["templating"]["list"]} >= {"env", "tenant"}


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda path: path.stem)
def test_postgres_panels_name_only_the_columns_the_observer_role_may_read(path: Path) -> None:
    for sql in _sql(json.loads(path.read_text(encoding="utf-8"))):
        assert "*" not in sql.replace("count(*)", "")
        assert not re.search(r"\b(request|result|idempotency_key|request_hash)\b", sql)


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
    assert len({alert["alert"] for alert in alerts}) == 12


def test_the_collector_keeps_every_field_the_log_helper_allows() -> None:
    config = yaml.safe_load((ROOT / "deploy/otel-collector/config.yaml").read_text(encoding="utf-8"))
    statements = config["processors"]["transform/allowlist"]["log_statements"][0]["statements"]
    keep = next(item for item in statements if item.startswith("keep_matching_keys(log.attributes"))
    match = re.search(r'"(.*)"\)$', keep)
    assert match is not None
    allowed = re.compile(match.group(1).replace("\\\\", "\\"))
    names = [*SAFE_FIELD_NAMES, *(f"sample{suffix}" for suffix in SAFE_FIELD_SUFFIXES)]

    assert [name for name in names if not allowed.fullmatch(name)] == []
