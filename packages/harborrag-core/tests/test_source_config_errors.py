from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated

import pytest

from harborrag_core.ingestion.errors import (
    SourceAuthenticationError,
    SourceItemNotFoundError,
    SourceUnavailableError,
)
from harborrag_core.sources import (
    AuthError,
    Capabilities,
    ConfigError,
    ConnectorConfig,
    Credential,
    DeletionMode,
    DiscoveryMode,
    ErrorCategory,
    FieldClass,
    Forbidden,
    ItemNotFound,
    PreflightCheck,
    PreflightResult,
    RateLimited,
    SyncPolicy,
    Transient,
    classify,
    diff_configs,
    field_classes,
)


class _Config(ConnectorConfig):
    projects: Annotated[list[str], FieldClass.SCOPE] = []
    field_map: Annotated[dict[str, str], FieldClass.RENDER] = {}
    page_size: Annotated[int, FieldClass.COSMETIC] = 100


class _Untagged(ConnectorConfig):
    projects: list[str] = []


def test_field_classes_read_annotations_and_reject_untagged_fields() -> None:
    assert field_classes(_Config) == {
        "projects": FieldClass.SCOPE,
        "field_map": FieldClass.RENDER,
        "page_size": FieldClass.COSMETIC,
    }
    with pytest.raises(ConfigError, match="projects"):
        field_classes(_Untagged)


def test_diff_groups_changes_by_what_they_mean() -> None:
    old = _Config(projects=["A"])
    assert not diff_configs(old, _Config(projects=["A"])).changed

    cosmetic = diff_configs(old, _Config(projects=["A"], page_size=50))
    assert cosmetic.cosmetic == {"page_size"}
    assert not cosmetic.requires_refetch

    render = diff_configs(old, _Config(projects=["A"], field_map={"cf_1": "name"}))
    assert render.requires_refetch and not render.requires_reconcile

    scope = diff_configs(old, _Config(projects=["A", "B"]))
    assert scope.scope == {"projects"}
    assert scope.requires_refetch and scope.requires_reconcile


def test_config_is_frozen_and_closed() -> None:
    with pytest.raises(ValueError):
        _Config(unknown=1)  # type: ignore[call-arg]


def test_errors_extend_the_ingestion_tree() -> None:
    assert issubclass(AuthError, SourceAuthenticationError)
    assert issubclass(ItemNotFound, SourceItemNotFoundError)
    assert issubclass(Transient, SourceUnavailableError)
    assert issubclass(RateLimited, SourceUnavailableError)
    assert ConfigError.retryable is False


def test_rate_limited_normalizes_retry_at_to_utc() -> None:
    later = datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=7)))
    error = RateLimited(later)
    assert error.retry_at == later.astimezone(UTC)
    assert "rate limited until" in str(error)
    with pytest.raises(ValueError):
        RateLimited(datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AuthError("x"), ErrorCategory.NEEDS_REAUTH),
        (Forbidden("x"), ErrorCategory.NEEDS_REAUTH),
        (SourceAuthenticationError("legacy"), ErrorCategory.NEEDS_REAUTH),
        (RateLimited(datetime(2026, 1, 1, tzinfo=UTC)), ErrorCategory.RATE_LIMITED),
        (Transient("x"), ErrorCategory.TRANSIENT),
        (TimeoutError(), ErrorCategory.TRANSIENT),
        (ConfigError("x"), ErrorCategory.CONFIG),
        (KeyError("x"), ErrorCategory.UNKNOWN),
    ],
)
def test_classify_maps_errors_to_runtime_actions(
    error: BaseException, category: ErrorCategory
) -> None:
    assert classify(error) is category


def test_capabilities_and_policy_validate() -> None:
    with pytest.raises(ValueError, match="record kind"):
        Capabilities(discovery=DiscoveryMode.SCAN, kinds=frozenset())
    with pytest.raises(ValueError, match="list_keys"):
        Capabilities(
            discovery=DiscoveryMode.SCAN,
            kinds=frozenset({"file"}),
            deletions=frozenset({DeletionMode.RECONCILE}),
            list_keys=False,
        )
    with pytest.raises(ValueError):
        SyncPolicy(overlap=timedelta(minutes=-1))


def test_preflight_result_reports_failures() -> None:
    result = PreflightResult((PreflightCheck("auth", True), PreflightCheck("project", False, "x")))
    assert not result.ok
    assert [check.name for check in result.failures] == ["project"]


def test_credential_hides_values_and_names_missing_fields() -> None:
    credential = Credential({"token": "secret"}, version=3)
    assert "secret" not in repr(credential)
    assert credential.require("token") == "secret"
    with pytest.raises(KeyError, match="refresh_token"):
        credential.require("refresh_token")
