"""Host processes translate a checkout's Compose env files the same way Compose does."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.config.checkout import (
    checkout_control_db_url,
    checkout_environment_name,
    control_db_url_from_compose,
    missing_control_db_values,
    read_compose_env_file,
)


def test_quoted_values_are_unquoted_like_compose(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "POSTGRES_USER=owner\n"
        'POSTGRES_PASSWORD="it\'s a p@ss/word"\n'
        "POSTGRES_DB='harbor'\n"
        "# comment\n"
        "POSTGRES_PORT=5433\n",
        encoding="utf-8",
    )

    values = read_compose_env_file(path)

    assert values["POSTGRES_PASSWORD"] == "it's a p@ss/word"
    assert values["POSTGRES_DB"] == "harbor"
    assert control_db_url_from_compose(values) == (
        "postgresql+asyncpg://owner:it%27s%20a%20p%40ss%2Fword@localhost:5433/harbor"
    )


def test_missing_values_are_named() -> None:
    assert missing_control_db_values({"POSTGRES_USER": "u"}) == ["POSTGRES_PASSWORD", "POSTGRES_DB"]
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD, POSTGRES_DB"):
        control_db_url_from_compose({"POSTGRES_USER": "u"})


def test_reader_credentials_can_be_selected() -> None:
    values = {"POSTGRES_DB": "harbor", "R_USER": "reader", "R_PASS": "pw"}

    url = control_db_url_from_compose(values, user_name="R_USER", password_name="R_PASS")

    assert url == "postgresql+asyncpg://reader:pw@localhost:5432/harbor"


def test_checkout_helpers_return_none_without_the_files(tmp_path: Path) -> None:
    assert checkout_control_db_url(tmp_path) is None
    assert checkout_environment_name(tmp_path) is None

    (tmp_path / "env").mkdir()
    (tmp_path / "env/.env.database").write_text(
        "POSTGRES_USER=o\nPOSTGRES_PASSWORD=p\nPOSTGRES_DB=d\n", encoding="utf-8"
    )
    (tmp_path / "env/.env.api").write_text("HARBORRAG_ENV=prod\n", encoding="utf-8")

    assert checkout_control_db_url(tmp_path) == "postgresql+asyncpg://o:p@localhost:5432/d"
    assert checkout_environment_name(tmp_path) == "prod"
