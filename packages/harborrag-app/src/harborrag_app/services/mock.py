from __future__ import annotations

from harborrag_app.services.base import AppResponse, BaseAppService


class MockAppService(BaseAppService):
    def health(self) -> AppResponse:
        from harborrag_runtime.composition import CompositionRoot

        return AppResponse(True, {"diagnostics": CompositionRoot.local().diagnostics()})

    def ingest_once(self) -> AppResponse:
        from harborrag_runtime.composition import CompositionRoot

        return AppResponse(True, CompositionRoot.local().run_mock_ingestion())
