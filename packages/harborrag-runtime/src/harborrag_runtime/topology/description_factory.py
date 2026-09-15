"""Open the pinned chat deployment only for an admitted, uncached parent call."""

from dataclasses import dataclass

from harborrag_adapters.models.chat import (
    ChatClientDependencies,
    ChatClientFactory,
    HarborChatClientConfig,
)
from harborrag_adapters.models.runtime import ResourceOwnership
from harborrag_adapters.topology import pinned_configuration
from harborrag_adapters.topology.descriptions import (
    DescriptionRun,
    LLMDescriptionGenerator,
    pin_rollup_model,
)
from harborrag_core.topology import ExtractionProfile
from harborrag_core.topology.derived import DescriptionOutput, DescriptionPacket
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.observability import build_model_telemetry


@dataclass(frozen=True)
class ConfiguredDescriptionGenerator:
    settings: RuntimeSettings
    profile: ExtractionProfile
    tenant_id: str
    document_id: str

    async def generate(self, packets: tuple[DescriptionPacket, ...]) -> DescriptionOutput:
        return (await self.generate_usage(packets)).output

    async def generate_usage(self, packets: tuple[DescriptionPacket, ...]) -> DescriptionRun:
        catalog = HarborChatClientConfig.from_file(self.settings.model_config_path)
        if self.settings.topology_parent_model:
            # The rollup has its own prompt, so it cannot pass the extraction pin.
            config, pricing = pin_rollup_model(catalog, self.settings.topology_parent_model)
        else:
            config, pricing = pinned_configuration(catalog, self.profile), None
        telemetry = build_model_telemetry(config, langfuse_enabled=self.settings.langfuse_enabled)
        try:
            client = ChatClientFactory.create_async(
                config,
                ChatClientDependencies(
                    telemetry=telemetry,
                    telemetry_ownership=ResourceOwnership.OWNED,
                ),
            )
        except BaseException:
            telemetry.close()
            raise
        try:
            return await LLMDescriptionGenerator(
                client,
                self.profile,
                self.tenant_id,
                self.document_id,
                self.settings.topology_operation_seconds,
                self.settings.topology_parent_max_output_tokens,
                pricing,
            ).generate_usage(packets)
        finally:
            await client.aclose()
