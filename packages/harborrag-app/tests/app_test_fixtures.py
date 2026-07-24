"""Application-layer test doubles kept outside the production package."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_app.api.auth.base import BaseTokenVerifier
from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_app.services.base import AppResponse, BaseAppService
from harborrag_core.contracts.errors import HarborAuthError


@dataclass(slots=True)
class MockTokenVerifier(BaseTokenVerifier):
    """Accept tokens of the form ``mock-<role>`` for auth boundary tests."""

    subject: str = "mock-user"

    def verify(self, token: str) -> Principal:
        prefix, _, role = token.partition("-")
        if prefix != "mock" or role not in ROLE_ORDER:
            raise HarborAuthError("invalid mock token")
        return Principal(subject=self.subject, role=role, token_kind="mock")


class MockAppService(BaseAppService):
    def health(self) -> AppResponse:
        return AppResponse(
            True,
            {
                "diagnostics": {
                    "mode": "development",
                    "runtime": {"provider": "app_test_double", "ready": True},
                }
            },
        )

    def ingest_once(self) -> AppResponse:
        return AppResponse(
            True,
            {
                "documents": ["mock://app/1"],
                "summary": {
                    "discovered": 1,
                    "loaded": 1,
                    "parsed": 1,
                    "indexed": 0,
                },
            },
        )

    async def start_ingestion(
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        manifest_id: str | None = None,
        generation_id: str | None = None,
        wait: bool = False,
    ) -> AppResponse:
        del wait
        return AppResponse(
            True,
            {
                "run": {
                    "run_id": run_id or "mock-run",
                    "tenant_id": tenant_id,
                    "connector_name": connector_name,
                    "manifest_id": manifest_id or "mock-manifest",
                    "generation_id": generation_id or "mock-generation",
                },
                "workflow": {"workflow_id": "mock-workflow"},
            },
        )

    async def ingestion_status(self, run_id: str) -> AppResponse:
        return AppResponse(True, {"status": {"run_id": run_id, "status": "completed"}})

    async def ingestion_result(self, run_id: str) -> AppResponse:
        return AppResponse(True, {"result": {"run_id": run_id, "status": "completed"}})

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
        *,
        artifact_ids: tuple[str, ...] = (),
        graceful: bool = True,
    ) -> AppResponse:
        del graceful
        return AppResponse(
            True,
            {"run_id": run_id, "action": action, "artifact_ids": list(artifact_ids)},
        )
