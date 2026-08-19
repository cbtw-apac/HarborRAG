"""Unit coverage for parse_and_normalize subprocess fallback and crash handling."""

from __future__ import annotations

from typing import Any

import pytest

from harborrag_runtime.temporal import ingestion_activities as activity_module
from harborrag_runtime.temporal.schemas import DocumentIngestionInput, RawCaptureResult

from .test_ingestion_activities import _artifact, _build_activities

pytestmark = pytest.mark.whitebox


@pytest.mark.asyncio
async def test_parse_and_normalize_falls_back_when_subprocess_serialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heartbeat(operation: Any, *, detail: object, **_kw: object) -> object:
        assert isinstance(detail, dict)
        assert detail["mode"] == "in-process-fallback"
        assert detail["fallback_reason"] == "spawn-unpicklable-args"
        return await operation

    async def isolated_subprocess(*_args: Any, **_kwargs: Any) -> object:
        raise activity_module.SubprocessSerializationError(
            "isolated subprocess serialization failed: TypeError: cannot pickle 'mappingproxy' object"
        )

    monkeypatch.setattr(activity_module, "heartbeat_while", heartbeat)
    monkeypatch.setattr(activity_module, "run_in_isolated_subprocess", isolated_subprocess)
    monkeypatch.setattr(activity_module, "last_heartbeat_detail", lambda: None)
    monkeypatch.setattr(activity_module, "to_capture_stage", lambda request: "capture-stage")
    monkeypatch.setattr(
        activity_module,
        "to_prepared_document",
        lambda request, result: ("prepared-result", request, result),
    )

    activities, _, _, _, observer, _ = _build_activities()
    document = DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), 0)
    raw = RawCaptureResult(document, "doc-1", "version-1", "METADATA_ONLY")

    parse_result: object = await activities.parse_and_normalize(raw)
    assert parse_result == ("prepared-result", document, "prepared-stage")
    assert ("subprocess_outcome", ("ParseAndNormalize", "serialization_fail")) in observer.records
    assert ("prepared", ("prepared-stage",)) in observer.records


@pytest.mark.asyncio
async def test_parse_and_normalize_raises_on_genuine_subprocess_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def isolated_subprocess(*_args: Any, **_kwargs: Any) -> object:
        raise activity_module.SubprocessCrashError("worker crashed")

    monkeypatch.setattr(activity_module, "run_in_isolated_subprocess", isolated_subprocess)
    monkeypatch.setattr(activity_module, "last_heartbeat_detail", lambda: None)
    monkeypatch.setattr(activity_module, "to_capture_stage", lambda request: "capture-stage")

    activities, _, _, _, observer, _ = _build_activities()
    document = DocumentIngestionInput("task-1", "tenant-1", "jira-main", _artifact(), 0)
    raw = RawCaptureResult(document, "doc-1", "version-1", "METADATA_ONLY")

    with pytest.raises(activity_module.SubprocessCrashError, match="worker crashed"):
        await activities.parse_and_normalize(raw)

    assert ("subprocess_outcome", ("ParseAndNormalize", "crash")) in observer.records
    assert not any(name == "prepared" for name, _ in observer.records)
