from __future__ import annotations

from harborrag_adapters.models.runtime.errors import ModelCallContext
from harborrag_adapters.models.runtime.telemetry import TelemetryDispatcher
from harborrag_adapters.models.runtime.telemetry_operation import ModelTelemetryOperation
from harborrag_core.models.embed import HarborEmbedRequest
from harborrag_core.models.errors import HarborEmbedError

from .configs import HarborEmbedProviderConfig
from .errors import normalize_exception


class EmbedExecutionContextMixin:
    """Build embedding telemetry and normalized provider failures."""

    telemetry: TelemetryDispatcher

    @staticmethod
    def _error(
        exc: Exception,
        logical: str,
        deployment: HarborEmbedProviderConfig,
        request: HarborEmbedRequest,
    ) -> HarborEmbedError:
        return normalize_exception(
            exc,
            ModelCallContext.for_deployment(
                deployment,
                logical_model=logical,
                request_id=request.metadata.request_id,
            ),
        )

    def _operation(
        self,
        logical: str,
        request: HarborEmbedRequest,
        model_alias: str,
    ) -> ModelTelemetryOperation:
        return ModelTelemetryOperation(
            self.telemetry,
            operation="embed",
            request=request,
            model_alias=model_alias,
            logical_model=logical,
        )
