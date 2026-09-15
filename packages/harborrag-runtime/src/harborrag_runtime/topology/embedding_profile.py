"""Single profile factory shared by contextual indexing and query composition."""

from dataclasses import asdict

from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.embed.configs import HarborEmbedModelConfig
from harborrag_adapters.topology.descriptions import (
    DESCRIPTION_CONTRACT_VERSION,
    DESCRIPTION_PROMPT,
)
from harborrag_core.topology.derived import ContextualIndexProfile, DescriptionOutput
from harborrag_core.topology.extraction import digest
from harborrag_engine.topology.parent_builder import PARENT_BUILDER_VERSION, ParentDescriptionPolicy
from harborrag_runtime.composition.resources import embedding_dimensions
from harborrag_runtime.config.settings import RuntimeSettings


def build_contextual_profile(
    settings: RuntimeSettings,
    config: HarborEmbedClientConfig | None = None,
) -> ContextualIndexProfile:
    selected = config or HarborEmbedClientConfig.from_file(settings.model_config_path)
    model = settings.embedding_model or selected.default_model
    canonical, logical = selected.model_for(model)
    dimensions = settings.embedding_dimensions or embedding_dimensions(selected, canonical)
    parent_policy = ParentDescriptionPolicy(
        max_fan_in=settings.topology_parent_max_fan_in,
        max_input_bytes=settings.topology_parent_max_input_bytes,
        max_input_tokens=settings.topology_parent_max_input_tokens,
        max_calls=settings.topology_parent_max_calls,
    )
    return ContextualIndexProfile(
        model=canonical,
        dimension=dimensions,
        deployment_revision=embedding_space_revision(logical),
        max_input_bytes=settings.topology_embedding_max_input_bytes,
        description_revision=digest(
            [
                DESCRIPTION_PROMPT,
                DESCRIPTION_CONTRACT_VERSION,
                DescriptionOutput.model_json_schema(),
                settings.topology_parent_max_output_tokens,
                PARENT_BUILDER_VERSION,
                asdict(parent_policy),
            ]
        ),
    )


def embedding_space_revision(model: HarborEmbedModelConfig) -> str:
    """Hash vector semantics without credentials, endpoints, or routing limits."""
    deployments = sorted(
        (
            {
                "provider": deployment.provider.value,
                "model": deployment.model,
                "deployment_name": deployment.deployment_name,
                "custom_llm_provider": deployment.custom_llm_provider,
                "expected_dimensions": deployment.expected_dimensions,
                "extra_litellm_params": deployment.extra_litellm_params,
            }
            for deployment in model.deployments
            if deployment.enabled
        ),
        key=lambda item: digest(item),
    )
    return digest(
        {
            "embedding_space": model.embedding_space,
            "deployments": deployments,
            "default_params": model.default_params.model_dump(mode="json"),
        }
    )
