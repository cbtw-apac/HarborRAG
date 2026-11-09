from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_core.testing.control_plane_fakes import (
    FakeSettingOverrideRepository,
    FakeSettingsRepository,
)
from harborrag_runtime.config.settings_seed import (
    DRIFT_EVENT,
    SEED_MARKER_KEY,
    seed_setting_overrides_once,
)

pytestmark = pytest.mark.asyncio


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


async def test_seeds_empty_database_once(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n  parsing.ocr.enabled: false\n",
    )
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()

    planned = await seed_setting_overrides_once(path, overrides=overrides, workspace=workspace)

    assert planned is not None
    assert {o.key: o.value for o in planned} == {
        "chunking.max_tokens": 384,
        "parsing.ocr.enabled": False,
    }
    assert await overrides.get_at("acme", "tenant", "acme") == {
        "chunking.max_tokens": 384,
        "parsing.ocr.enabled": False,
    }
    assert SEED_MARKER_KEY in workspace.settings.data


async def test_second_boot_with_unchanged_file_does_not_reseed(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    await seed_setting_overrides_once(path, overrides=overrides, workspace=workspace)

    result = await seed_setting_overrides_once(path, overrides=overrides, workspace=workspace)

    assert result is None
    assert await overrides.count_all() == 1


async def test_changed_file_logs_drift_without_reimporting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    await seed_setting_overrides_once(path, overrides=overrides, workspace=workspace)

    path.write_text("tenant: acme\nsettings:\n  chunking.max_tokens: 999\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.config.settings_seed"):
        result = await seed_setting_overrides_once(path, overrides=overrides, workspace=workspace)

    assert result is None
    assert DRIFT_EVENT in caplog.text
    # The DB keeps the originally seeded value; the edited file was never re-read into it.
    assert (await overrides.get_at("acme", "tenant", "acme"))["chunking.max_tokens"] == 384


async def test_missing_file_is_a_noop(tmp_path: Path) -> None:
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()

    result = await seed_setting_overrides_once(
        tmp_path / "missing.yaml", overrides=overrides, workspace=workspace
    )

    assert result is None
    assert await overrides.count_all() == 0


async def test_preexisting_rows_without_a_marker_block_seeding(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n")
    overrides = FakeSettingOverrideRepository()
    from harborrag_core.domain.setting_override import SettingOverride

    await overrides.put(
        SettingOverride(
            tenant_id="acme",
            scope_level="tenant",
            scope_id="acme",
            key="chunking.max_tokens",
            value=700,
        ),
        expected_version=None,
    )
    workspace = FakeSettingsRepository()

    result = await seed_setting_overrides_once(path, overrides=overrides, workspace=workspace)

    assert result is None
    assert (await overrides.get_at("acme", "tenant", "acme"))["chunking.max_tokens"] == 700
    assert SEED_MARKER_KEY not in workspace.settings.data


async def test_unknown_key_fails_validation_without_writing_a_marker(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  not.a.real.key: 1\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()

    result = await seed_setting_overrides_once(path, overrides=overrides, workspace=workspace)

    assert result is None
    assert await overrides.count_all() == 0
    assert SEED_MARKER_KEY not in workspace.settings.data
