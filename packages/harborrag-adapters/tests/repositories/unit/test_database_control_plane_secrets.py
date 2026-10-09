from __future__ import annotations

from typing import cast

from harborrag_adapters.repositories.database.control_plane.secrets import SqlSecretsRepository
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory


def test_the_secrets_repository_never_shows_its_encryption_key() -> None:
    repo = SqlSecretsRepository(
        sessions=cast(SessionFactory, None),
        encryption_key="super-secret-encryption-key-0123456789abcdef",
    )

    assert "super-secret-encryption-key" not in repr(repo)
    assert "SqlSecretsRepository(" in repr(repo)
