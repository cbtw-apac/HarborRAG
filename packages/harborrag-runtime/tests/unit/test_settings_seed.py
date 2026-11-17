from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_core.testing.control_plane_fakes import (
    FakeLeaseRepository,
    FakeSettingOverrideRepository,
    FakeSettingScopeRevisionRepository,
    FakeSettingsRepository,
    FakeSettingsSeedRepository,
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
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)

    planned = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

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
    assert await revisions.get_record("acme", "tenant", "acme") == {
        "revision": 1,
        "source": "seed",
        "actor": None,
    }


async def test_second_boot_with_unchanged_file_does_not_reseed(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)
    await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    result = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    assert result is None
    assert await overrides.count_all() == 1
    # Restarting with the same file never bumps the revision again.
    assert await revisions.get("acme", "tenant", "acme") == 1


async def test_changed_file_logs_drift_without_reimporting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)
    await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    path.write_text("tenant: acme\nsettings:\n  chunking.max_tokens: 999\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.config.settings_seed"):
        result = await seed_setting_overrides_once(
            path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
        )

    assert result is None
    assert DRIFT_EVENT in caplog.text
    # The DB keeps the originally seeded value; the edited file was never re-read into it.
    assert (await overrides.get_at("acme", "tenant", "acme"))["chunking.max_tokens"] == 384
    assert await revisions.get("acme", "tenant", "acme") == 1


async def test_drift_log_names_the_key_that_changed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write_config(
        tmp_path,
        "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n  parsing.ocr.enabled: false\n",
    )
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)
    await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    path.write_text(
        "tenant: acme\nsettings:\n  chunking.max_tokens: 999\n  parsing.ocr.enabled: false\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.config.settings_seed"):
        await seed_setting_overrides_once(
            path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
        )

    assert "keys=chunking.max_tokens" in caplog.text
    assert "parsing.ocr.enabled" not in caplog.text.split("keys=")[1].split(" ")[0]


async def test_a_second_process_racing_the_first_seed_is_skipped_not_overwritten(
    tmp_path: Path,
) -> None:
    """Two processes starting at once must not both reach the write path.

    Without the lease, both could read "no marker, no rows" before either
    writes, and the slower process's stale plan could still land after the
    faster one already seeded (or after an operator edited settings through
    the now-serving API). The lease means only the first holder ever gets
    past the acquire, so the second attempt is a no-op, not a second write.
    """
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  chunking.max_tokens: 384\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)

    first = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )
    # A second process, racing the first, tries with a different holder while
    # the lease p1 just took is still live.
    second = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p2"
    )

    assert first is not None
    assert second is None
    assert await overrides.count_all() == 1
    assert await revisions.get("acme", "tenant", "acme") == 1


async def test_missing_file_is_a_noop(tmp_path: Path) -> None:
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)

    result = await seed_setting_overrides_once(
        tmp_path / "missing.yaml", overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
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
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)

    result = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    assert result is None
    assert (await overrides.get_at("acme", "tenant", "acme"))["chunking.max_tokens"] == 700
    assert SEED_MARKER_KEY not in workspace.settings.data


async def test_unknown_key_fails_validation_without_writing_a_marker(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  not.a.real.key: 1\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)

    result = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    assert result is None
    assert await overrides.count_all() == 0
    assert SEED_MARKER_KEY not in workspace.settings.data


@pytest.mark.parametrize(
    "duration",
    ["PT0S", "PT0.5S", "PT30S", "P8D", "not-a-duration"],
)
async def test_an_invalid_activity_timeout_fails_validation_without_seeding(
    tmp_path: Path, duration: str
) -> None:
    """The registry's `string` type check lets any of these through; only the
    ISO 8601 parse and Temporal's 60s-7d bounds catch them, and they must do
    so before the value is stored -- otherwise every connection in the
    tenant with no connection-level override fails every later submission.
    """
    path = _write_config(
        tmp_path, f"tenant: acme\nsettings:\n  ingestion.activity_timeout: {duration!r}\n"
    )
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)

    result = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    assert result is None
    assert await overrides.count_all() == 0
    assert SEED_MARKER_KEY not in workspace.settings.data


async def test_a_valid_activity_timeout_seeds_normally(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "tenant: acme\nsettings:\n  ingestion.activity_timeout: P1D\n")
    overrides = FakeSettingOverrideRepository()
    workspace = FakeSettingsRepository()
    revisions = FakeSettingScopeRevisionRepository()
    leases = FakeLeaseRepository()
    seed_writer = FakeSettingsSeedRepository(overrides, revisions, workspace)

    result = await seed_setting_overrides_once(
        path, overrides=overrides, workspace=workspace, leases=leases, seed_writer=seed_writer, holder="p1"
    )

    assert result is not None
    assert (await overrides.get_at("acme", "tenant", "acme"))["ingestion.activity_timeout"] == "P1D"
