"""The SDK config file rejects keys under ``runtime:`` that are not runtime settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.sdk import HarborRAGConfig

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, runtime: str) -> Path:
    path = tmp_path / "sdk.yaml"
    path.write_text(f"runtime:\n{runtime}", encoding="utf-8")
    return path


def test_sdk_config_misspelled_runtime_keys_are_named(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "  contol_db_url: postgresql+asyncpg://demo@database.invalid/control\n  tennant_id: DEMO\n",
    )

    with pytest.raises(ValueError, match="contol_db_url, tennant_id"):
        HarborRAGConfig.from_file(path)


def test_sdk_config_uppercase_runtime_key_is_unknown(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="TENANT_ID"):
        HarborRAGConfig.from_file(_write(tmp_path, "  TENANT_ID: DEMO\n"))


def test_sdk_config_known_runtime_keys_load(tmp_path: Path) -> None:
    config = HarborRAGConfig.from_file(
        _write(tmp_path, "  tenant_id: DEMO\n  memory_retention_days_user: 30\n")
    )

    assert (config.runtime.tenant_id, config.runtime.memory_retention_days_user) == ("DEMO", 30)
