"""The API exports each source's freshness from a snapshot it refreshes in the background."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from pydantic import SecretStr

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.metrics import (
    SOURCE_LAST_RUN_STATUS_METRIC,
    SOURCE_LAST_SUCCESS_METRIC,
    SOURCE_STATE_REFRESH_METRIC,
    ApiMetrics,
)
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.ports.source_freshness import SourceFreshness, SourceFreshnessReport

pytestmark = pytest.mark.unit

_SCRAPE = "scrape-token-0123456789abcdef"
_LAST_SUCCESS = datetime(2026, 11, 27, 3, 22, 29, tzinfo=UTC)
_SYNCED = SourceFreshness(
    tenant_id="tenant-a",
    source_scope_id="scope-synced",
    connector_type="jira",
    last_success_at=_LAST_SUCCESS,
    last_run_status=IngestionTaskState.PARTIAL,
)
_NEVER_SYNCED = SourceFreshness(
    tenant_id="tenant-b",
    source_scope_id="scope-new",
    connector_type="local",
    last_success_at=None,
    last_run_status=IngestionTaskState.PENDING,
)
_REPORT = SourceFreshnessReport(sources=(_SYNCED, _NEVER_SYNCED), source_count=2)
_SOURCE_GAUGES = (SOURCE_LAST_SUCCESS_METRIC, SOURCE_LAST_RUN_STATUS_METRIC)


def _samples(exposition: str, name: str) -> list[tuple[dict[str, str], float]]:
    families = {family.name: family for family in text_string_to_metric_families(exposition)}
    return [(sample.labels, sample.value) for sample in families[name].samples]


def _help(exposition: str, name: str) -> str:
    families = {family.name: family for family in text_string_to_metric_families(exposition)}
    return families[name].documentation


def _labels(source: SourceFreshness) -> dict[str, str]:
    return {
        "tenant": source.tenant_id,
        "source_scope_id": source.source_scope_id,
        "connector_type": source.connector_type,
    }


class _Reader:
    """A freshness reader that answers each read with the next outcome in turn."""

    def __init__(self, *outcomes: SourceFreshnessReport | Exception) -> None:
        self._outcomes = list(outcomes)

    async def source_freshness(self, *, limit: int) -> SourceFreshnessReport:
        del limit
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _HangingReader:
    async def source_freshness(self, *, limit: int) -> SourceFreshnessReport:
        del limit
        await asyncio.Event().wait()
        raise AssertionError("the read was never meant to finish")


@pytest.fixture
def app_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    """Capture the API module's events, which an earlier app build stops at ``harborrag``."""
    monkeypatch.setattr(logging.getLogger("harborrag"), "propagate", True)
    caplog.set_level(logging.INFO, logger="harborrag.app.api.app")
    return caplog


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.__dict__.get("event") == event]


def test_each_source_exports_its_last_success_and_its_latest_status() -> None:
    metrics = ApiMetrics(version="test")

    metrics.record_source_freshness(_REPORT.sources, refreshed_at=1_700_000_000.0)
    exposition = metrics.render().decode()

    assert sorted(
        _samples(exposition, SOURCE_LAST_SUCCESS_METRIC), key=lambda sample: sample[1]
    ) == [(_labels(_NEVER_SYNCED), 0.0), (_labels(_SYNCED), _LAST_SUCCESS.timestamp())]
    assert sorted(
        _samples(exposition, SOURCE_LAST_RUN_STATUS_METRIC), key=lambda sample: sample[0]["tenant"]
    ) == [
        ({**_labels(_SYNCED), "status": "partial"}, 1.0),
        ({**_labels(_NEVER_SYNCED), "status": "pending"}, 1.0),
    ]
    assert _samples(exposition, SOURCE_STATE_REFRESH_METRIC) == [({}, 1_700_000_000.0)]
    for name in _SOURCE_GAUGES:
        assert "every api replica reports the same value; aggregate with max" in (
            _help(exposition, name).lower()
        )


def test_no_source_gauge_has_a_sample_before_the_first_refresh() -> None:
    exposition = ApiMetrics(version="test").render().decode()

    for name in (*_SOURCE_GAUGES, SOURCE_STATE_REFRESH_METRIC):
        assert _samples(exposition, name) == []


def test_a_refresh_replaces_every_source_at_once() -> None:
    metrics = ApiMetrics(version="test")
    metrics.record_source_freshness(_REPORT.sources, refreshed_at=1.0)

    metrics.record_source_freshness([_NEVER_SYNCED], refreshed_at=2.0)
    exposition = metrics.render().decode()

    assert _samples(exposition, SOURCE_LAST_SUCCESS_METRIC) == [(_labels(_NEVER_SYNCED), 0.0)]
    assert _samples(exposition, SOURCE_STATE_REFRESH_METRIC) == [({}, 2.0)]


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_last_snapshot(
    app_log: pytest.LogCaptureFixture,
) -> None:
    metrics = ApiMetrics(version="test")
    metrics.record_source_freshness(_REPORT.sources, refreshed_at=1.0)
    before = metrics.render().decode()

    await api_app._refresh_source_freshness_once(
        _Reader(HarborUnavailableError("control database is unreachable")), metrics
    )

    after = metrics.render().decode()
    for name in (*_SOURCE_GAUGES, SOURCE_STATE_REFRESH_METRIC):
        assert _samples(after, name) == _samples(before, name)
    [failure] = _events(app_log, "observability.source_metrics_refresh_failed")
    assert failure.levelno == logging.WARNING
    assert failure.__dict__["error_type"] == "HarborUnavailableError"


@pytest.mark.asyncio
async def test_a_refresh_that_times_out_keeps_the_last_snapshot(
    app_log: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_app, "_SOURCE_FRESHNESS_REFRESH_TIMEOUT_SECONDS", 0.01)
    metrics = ApiMetrics(version="test")
    metrics.record_source_freshness([_SYNCED], refreshed_at=1.0)

    await api_app._refresh_source_freshness_once(_HangingReader(), metrics)

    exposition = metrics.render().decode()
    assert _samples(exposition, SOURCE_STATE_REFRESH_METRIC) == [({}, 1.0)]
    assert _samples(exposition, SOURCE_LAST_SUCCESS_METRIC) == [
        (_labels(_SYNCED), _LAST_SUCCESS.timestamp())
    ]
    [failure] = _events(app_log, "observability.source_metrics_refresh_failed")
    assert failure.levelno == logging.WARNING
    assert failure.__dict__["error_code"] == "timeout"


@pytest.mark.asyncio
async def test_a_refresh_cut_short_by_the_cap_logs_both_counts(
    app_log: pytest.LogCaptureFixture,
) -> None:
    metrics = ApiMetrics(version="test")

    await api_app._refresh_source_freshness_once(
        _Reader(SourceFreshnessReport(sources=(_SYNCED,), source_count=1500)), metrics
    )

    [truncated] = _events(app_log, "observability.source_metrics_truncated")
    assert truncated.levelno == logging.WARNING
    assert truncated.__dict__["source_count"] == 1500
    assert truncated.__dict__["exported_source_count"] == 1
    assert _samples(metrics.render().decode(), SOURCE_LAST_SUCCESS_METRIC) == [
        (_labels(_SYNCED), _LAST_SUCCESS.timestamp())
    ]


class _FreshnessService(MockAppService):
    """The route-test service, reading freshness once, failing once, then hanging."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.hanging = threading.Event()
        self.cancelled = threading.Event()

    async def source_freshness(self, *, limit: int) -> SourceFreshnessReport:
        del limit
        self.reads += 1
        if self.reads == 1:
            return _REPORT
        if self.reads == 2:
            raise HarborUnavailableError("control database is unreachable")
        self.hanging.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("the read was never meant to finish")


def test_the_api_refreshes_at_startup_and_stops_the_refresh_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FreshnessService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    monkeypatch.setattr(api_app, "_SOURCE_FRESHNESS_REFRESH_INTERVAL_SECONDS", 0.0)
    settings = ApiSettings(ops_port=9465, metrics_scrape_token=SecretStr(_SCRAPE))

    with TestClient(create_fastapi_app(settings)) as client:
        assert service.hanging.wait(timeout=10)
        response = client.get(
            "http://testserver:9465/metrics", headers={"Authorization": f"Bearer {_SCRAPE}"}
        )

    assert service.cancelled.is_set()
    assert response.status_code == 200
    assert sorted(
        _samples(response.text, SOURCE_LAST_SUCCESS_METRIC), key=lambda sample: sample[1]
    ) == [(_labels(_NEVER_SYNCED), 0.0), (_labels(_SYNCED), _LAST_SUCCESS.timestamp())]
    assert len(_samples(response.text, SOURCE_STATE_REFRESH_METRIC)) == 1
