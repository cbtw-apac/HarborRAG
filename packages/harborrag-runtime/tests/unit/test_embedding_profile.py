"""Derived-vector identities describe vector semantics, not deployment plumbing."""

from types import SimpleNamespace

from harborrag_adapters.models.embed.configs import (
    HarborEmbedClientConfig,
    HarborEmbedModelConfig,
    HarborEmbedProviderConfig,
)
from harborrag_adapters.models.embed.registry import HarborEmbedProvider
from harborrag_runtime.topology import embedding_profile
from harborrag_runtime.topology.embedding_profile import build_contextual_profile


def _config(
    *,
    api_key: str = "secret-a",
    weight: float = 1,
    rpm: int | None = None,
    model: str = "openai/text-embedding-test",
) -> HarborEmbedClientConfig:
    return HarborEmbedClientConfig(
        default_model="primary",
        models={
            "primary": HarborEmbedModelConfig(
                embedding_space="shared-space-v1",
                deployments=(
                    HarborEmbedProviderConfig(
                        name="primary-deployment",
                        provider=HarborEmbedProvider.OPENAI,
                        model=model,
                        api_key=api_key,
                        api_base="https://embedding.example/v1",
                        weight=weight,
                        rpm=rpm,
                        expected_dimensions=3,
                    ),
                ),
            )
        },
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        embedding_model=None,
        embedding_dimensions=3,
        model_config_path="unused.yaml",
        topology_embedding_max_input_bytes=8000,
        topology_parent_enabled=False,
        topology_parent_max_output_tokens=1024,
        topology_parent_max_fan_in=8,
        topology_parent_max_input_bytes=24000,
        topology_parent_max_input_tokens=6000,
        topology_parent_max_calls=64,
    )


def test_profile_ignores_credentials_endpoints_and_routing_limits() -> None:
    baseline = build_contextual_profile(_settings(), _config())
    operational_change = build_contextual_profile(
        _settings(), _config(api_key="rotated", weight=2, rpm=100)
    )
    assert operational_change.fingerprint == baseline.fingerprint
    assert operational_change.parent_fingerprint == baseline.parent_fingerprint


def test_profile_changes_when_embedding_model_changes() -> None:
    baseline = build_contextual_profile(_settings(), _config())
    model_change = build_contextual_profile(
        _settings(), _config(model="openai/text-embedding-next")
    )
    assert model_change.fingerprint != baseline.fingerprint
    assert model_change.index_name != baseline.index_name


def test_parent_profile_folds_the_native_description_contract(monkeypatch) -> None:
    baseline = build_contextual_profile(_settings(), _config())
    monkeypatch.setattr(
        embedding_profile,
        "DESCRIPTION_CONTRACT_VERSION",
        "parent-description-next",
    )
    changed = build_contextual_profile(_settings(), _config())

    assert changed.fingerprint == baseline.fingerprint
    assert changed.parent_fingerprint != baseline.parent_fingerprint


def test_parent_profile_folds_the_completion_budget() -> None:
    baseline_settings = _settings()
    baseline = build_contextual_profile(baseline_settings, _config())
    changed_settings = SimpleNamespace(**vars(baseline_settings))
    changed_settings.topology_parent_max_output_tokens = 2048
    changed = build_contextual_profile(changed_settings, _config())

    assert changed.fingerprint == baseline.fingerprint
    assert changed.parent_fingerprint != baseline.parent_fingerprint


def test_parent_profile_folds_the_reducer_policy() -> None:
    baseline_settings = _settings()
    baseline = build_contextual_profile(baseline_settings, _config())
    changed_settings = SimpleNamespace(**vars(baseline_settings))
    changed_settings.topology_parent_max_fan_in = 4
    changed = build_contextual_profile(changed_settings, _config())

    assert changed.fingerprint == baseline.fingerprint
    assert changed.parent_fingerprint != baseline.parent_fingerprint
