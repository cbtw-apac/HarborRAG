"""Production BaseRuntimeService (ST8).

diagnostics() reports what composition verified at boot: control-DB
reachability and the stamped migration version. Real ingestion submission
lands in M2; run_mock_ingestion still delegates to the mock pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harborrag_runtime.services.base import BaseRuntimeService


@dataclass(slots=True)
class ProductionRuntimeService(BaseRuntimeService):
    """Runtime facade backed by the control-plane DB composition."""

    control_db: dict[str, Any] = field(default_factory=dict)

    def diagnostics(self) -> dict[str, object]:
        """Component health: provider tag, readiness, and control-DB probe."""
        return {
            "provider": "production_runtime",
            "ready": bool(self.control_db.get("ping") == "ok"),
            "control_db": dict(self.control_db),
        }

    def run_mock_ingestion(self) -> dict[str, object]:
        """Deterministic mock ingestion (real job submission arrives in M2)."""
        from harborrag_runtime.composition import CompositionRoot

        return CompositionRoot.local().run_mock_ingestion()
