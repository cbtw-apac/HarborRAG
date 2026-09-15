from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from harborrag_core.topology import ExtractionProfile, TopologyPolicy
from harborrag_runtime.config.errors import GraphBuildConfigurationError
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology.configuration import GraphBuildConfigSynchronizer


def test_repository_graph_build_policy_is_executable() -> None:
    config = GraphBuildConfig.from_file(Path("config/graph_build.yaml"))

    tenant = config.tenants[0]
    assert tenant.tenant_id == "DEFAULT"
    assert tenant.llm_enabled
    assert tenant.budget.daily_token_cap == 200_000_000
    assert tenant.budget.daily_cost_usd == Decimal("15.00")
    assert tenant.budget.max_job_attempts == 10
    assert tenant.sources[0].extraction.max_output_tokens == 2048
    assert config.runtime.derived.parent_max_fan_in == 8
    assert config.runtime.derived.parent_max_input_bytes == 24_000
    assert config.runtime.derived.parent_max_input_tokens == 6_000
    assert config.runtime.derived.parent_max_calls == 256
    assert config.runtime.derived.parent_max_output_tokens == 512
    assert "embedding_operation_cost_usd" not in config.runtime.model_dump()


def test_yaml_is_the_single_authority_for_graph_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_TOPOLOGY_DERIVED_ENABLED", "false")
    monkeypatch.setenv("HARBORRAG_TOPOLOGY_OPERATION_SECONDS", "45")
    settings = RuntimeSettings(graph_build_config_path=Path("config/graph_build.yaml"))
    effective = GraphBuildConfig.from_settings(settings).effective_settings(settings)

    assert effective.topology_derived_enabled is True
    assert effective.topology_operation_seconds == 180
    assert effective.topology_job_seconds == 3600
    assert effective.topology_parent_max_fan_in == 8
    assert effective.topology_parent_max_input_bytes == 24_000
    assert effective.topology_parent_max_input_tokens == 6_000
    assert effective.topology_parent_max_calls == 256


@pytest.mark.parametrize(
    "payload",
    (
        "unknown: true\n",
        "version: 1\n",
        "version: 1\nversion: 1\n",
        "tenants:\n  - tenant_id: one\n    mode: sometimes\n",
        "tenants:\n  - tenant_id: one\n    budget:\n      max_concurrency: '2'\n",
        "tenants:\n  - tenant_id: one\n    sources:\n      - source_scope_id: source\n        projection_revision: semantic-v6\n",
    ),
)
def test_graph_build_loader_rejects_ambiguous_or_coerced_policy(tmp_path, payload) -> None:
    path = tmp_path / "graph_build.yaml"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(GraphBuildConfigurationError):
        GraphBuildConfig.from_file(path)


class _Repository:
    def __init__(self, policy: TopologyPolicy | None = None) -> None:
        self.policy = policy
        self.indexing = []
        self.policies = []

    async def configure_indexing(self, config):
        self.indexing.append(config)
        return Mock(config=config)

    async def get_policy(self, tenant_id, source_scope_id):
        del tenant_id, source_scope_id
        return self.policy

    async def configure_policy(self, policy):
        self.policy = policy
        self.policies.append(policy)
        return len(self.policies)

    async def reconcile(self, tenant_id):
        del tenant_id
        return 0


class _Profiles:
    def __init__(self) -> None:
        self.calls = 0

    def build(self, source):
        del source
        self.calls += 1
        return _profile()


def _profile() -> ExtractionProfile:
    return ExtractionProfile(
        model="primary",
        deployment_revision="deployment-1",
        prompt_digest="prompt-1",
    )


@pytest.mark.asyncio
async def test_deterministic_mode_hard_disables_llm_without_loading_a_model(tmp_path) -> None:
    path = tmp_path / "graph_build.yaml"
    path.write_text(
        """tenants:
  - tenant_id: tenant-a
    mode: deterministic
    sources:
      - source_scope_id: source-a
""",
        encoding="utf-8",
    )
    existing = TopologyPolicy(
        tenant_id="tenant-a",
        source_scope_id="source-a",
        enabled=True,
        profile=_profile(),
    )
    repository = _Repository(existing)
    profiles = _Profiles()

    report = await GraphBuildConfigSynchronizer(
        GraphBuildConfig.from_file(path), repository, profiles
    ).apply()

    assert profiles.calls == 0
    assert repository.indexing[0].enabled is False
    assert repository.policies[0].enabled is False
    assert report.policies_disabled == 1


@pytest.mark.asyncio
async def test_llm_mode_maps_budget_and_source_policy(tmp_path) -> None:
    path = tmp_path / "graph_build.yaml"
    path.write_text(
        """tenants:
  - tenant_id: tenant-a
    mode: llm
    spending_paused: true
    budget:
      max_concurrency: 2
      daily_token_cap: 9000
      daily_cost_usd: "12.50"
      reservation_seconds: 60
    sources:
      - source_scope_id: source-a
""",
        encoding="utf-8",
    )
    repository = _Repository()
    profiles = _Profiles()

    report = await GraphBuildConfigSynchronizer(
        GraphBuildConfig.from_file(path), repository, profiles
    ).apply()

    indexing = repository.indexing[0]
    assert indexing.enabled is True
    assert indexing.spending_paused is True
    assert indexing.budgets.daily_cost_usd == Decimal("12.50")
    assert repository.policies[0].enabled is True
    assert profiles.calls == 1
    assert report.policies_enabled == 1
