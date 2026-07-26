from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.workflow_control.client import AppService
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.retrieval import (
    RetrievalDiagnostics,
    RuntimeRetrievalReport,
)


class FakeComposition:
    def diagnostics(self):
        return {"runtime": {"ready": True}}

    async def aclose(self) -> None:
        return None


class FakeRetrievalService:
    def __init__(self) -> None:
        self.calls = []
        self.closed = False

    async def retrieve(self, query, *, tenant_id, top_k):
        self.calls.append((query, tenant_id, top_k))
        return RuntimeRetrievalReport(
            request_id="retrieval-safe",
            results=(
                RetrievalResult(
                    "revision-hash",
                    "private document text",
                    0.75,
                    {"retrieval_source": "qdrant"},
                ),
            ),
            diagnostics=RetrievalDiagnostics(3, 4, 2, 1, False, 12.5),
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_app_retrieval_omits_content_by_default_and_closes_resources() -> None:
    retrieval = FakeRetrievalService()

    async def factory(settings):
        del settings
        return retrieval

    service = AppService(
        FakeComposition(),  # type: ignore[arg-type]
        retrieval_factory=factory,  # type: ignore[arg-type]
    )

    response = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        top_k=3,
    )
    await service.aclose()

    assert response.ok is True
    assert response.data["results"] == [
        {
            "rank": 1,
            "id": "revision-hash",
            "score": 0.75,
            "source": "qdrant",
        }
    ]
    assert "private document text" not in str(response.data)
    assert retrieval.calls == [("release acceptance", "tenant-1", 3)]
    assert retrieval.closed is True


@pytest.mark.asyncio
async def test_app_retrieval_includes_content_only_when_requested() -> None:
    retrieval = FakeRetrievalService()

    async def factory(settings):
        del settings
        return retrieval

    service = AppService(
        FakeComposition(),  # type: ignore[arg-type]
        retrieval_factory=factory,  # type: ignore[arg-type]
    )

    response = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        include_content=True,
    )

    assert response.data["results"][0]["content"] == "private document text"


def test_retrieval_cli_has_secret_safe_json_by_default(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    exit_code = cli.main(
        [
            "retrieve",
            "release acceptance",
            "--tenant",
            "tenant-1",
            "--top-k",
            "3",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["data"]["diagnostics"]["vector_hits"] == 1
    assert "content" not in payload["data"]["results"][0]


def test_unexpected_provider_error_message_is_not_public(monkeypatch, capsys) -> None:
    def fail_to_build():
        raise RuntimeError("private-url?token=private")

    monkeypatch.setattr(cli_runner, "runtime_app_service", fail_to_build)

    exit_code = cli.main(["retrieve", "query", "--tenant", "tenant-1", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["error"] == "RuntimeError"
    assert "private" not in str(payload)
