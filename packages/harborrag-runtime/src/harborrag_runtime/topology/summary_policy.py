"""Freeze all model-affecting summary policy without provider calls."""

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.topology import default_extraction_profile
from harborrag_adapters.topology.descriptions import (
    DESCRIPTION_CONTRACT_VERSION,
    DESCRIPTION_PROMPT,
)
from harborrag_core.summaries import SummaryPolicy
from harborrag_core.topology.extraction import digest
from harborrag_runtime.config.settings import RuntimeSettings


def build_summary_policy(
    settings: RuntimeSettings, catalog: HarborChatClientConfig | None = None
) -> SummaryPolicy:
    catalog = catalog or HarborChatClientConfig.from_file(settings.model_config_path)
    profile = default_extraction_profile(catalog, settings.topology_parent_model)
    return SummaryPolicy(
        model_fingerprint=digest(
            [
                profile.model,
                profile.deployment_revision,
                profile.temperature,
                profile.reasoning_effort,
                settings.topology_parent_max_output_tokens,
                DESCRIPTION_CONTRACT_VERSION,
                DESCRIPTION_PROMPT,
            ]
        ),
        max_fan_in=settings.topology_parent_max_fan_in,
        max_input_bytes=settings.topology_parent_max_input_bytes,
        max_input_tokens=settings.topology_parent_max_input_tokens,
        max_calls=settings.topology_parent_max_calls,
        debounce_seconds=settings.summary_debounce_seconds,
        max_wait_seconds=settings.summary_max_wait_seconds,
        tenant_enabled=settings.summary_tenant_enabled,
    )
