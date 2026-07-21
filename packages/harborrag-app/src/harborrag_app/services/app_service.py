"""Production BaseAppService over a composed runtime (ST8).

M0 surface is health() + ingest_once() (the ABC's current contract); the
per-resource use-case methods grow with the M1+ routes.
"""

from __future__ import annotations

from harborrag_runtime.composition import CompositionRoot

from harborrag_app.services.base import AppResponse, BaseAppService


class AppService(BaseAppService):
    """App-facing use-case facade bound to one composition root."""

    def __init__(self, composition: CompositionRoot) -> None:
        """Bind the service to an already-built composition."""
        self._composition = composition

    def health(self) -> AppResponse:
        """Runtime + engine diagnostics; ok=False when the runtime isn't ready."""
        diagnostics = self._composition.diagnostics()
        runtime = diagnostics.get("runtime")
        ready = bool(runtime.get("ready")) if isinstance(runtime, dict) else False
        return AppResponse(
            ok=ready,
            data={"diagnostics": diagnostics},
            error=None if ready else "runtime not ready",
        )

    def ingest_once(self) -> AppResponse:
        """Run the deterministic mock ingestion (real submission lands in M2)."""
        return AppResponse(True, self._composition.run_mock_ingestion())
