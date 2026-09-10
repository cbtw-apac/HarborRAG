"""SqlSecretsRepository: SecretsPort over the `secrets` table.

Values are Fernet-encrypted at rest; the ref is an opaque uuid4-derived
string with no relationship to the plaintext, and no route/service code
outside this file ever sees a Fernet key or ciphertext.

Every read and write is tenant-scoped. A ref is stored against the tenant
that put it and resolve/delete filter on `(ref, tenant_id)`, so a ref that
leaks across a tenant boundary is indistinguishable from an unknown ref --
the shared Fernet key never turns it back into plaintext for the wrong
tenant.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass, field
from uuid import uuid4

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

from harborrag_core.contracts.errors import HarborNotFoundError, HarborSecretDecryptionError

from .mapping import utc_now
from .schemas import SecretRefRow
from .session import SessionFactory

logger = logging.getLogger("harborrag.adapters.control_plane.secrets")


def _fernet_key(raw_key: str) -> bytes:
    """Derive a valid 32-byte urlsafe-base64 Fernet key from an arbitrary secret string."""
    return base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode()).digest())


@dataclass(slots=True)
class SqlSecretsRepository:
    """SecretsPort over the secrets table; ciphertext only ever leaves this class decrypted."""

    sessions: SessionFactory
    encryption_key: str
    created_by: str = "harborrag-adapters"
    _fernet: Fernet = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._fernet = Fernet(_fernet_key(self.encryption_key))

    async def put(self, value: str, *, tenant_id: str) -> str:
        """Encrypt and store a raw value for a tenant; return a fresh opaque ref."""
        ref = f"secret://db/{uuid4().hex}"
        ciphertext = self._fernet.encrypt(value.encode())
        async with self.sessions.begin() as session:
            session.add(
                SecretRefRow(
                    ref=ref,
                    tenant_id=tenant_id,
                    provider="db",
                    created_by=self.created_by,
                    created_at=utc_now(),
                    ciphertext=ciphertext,
                )
            )
        return ref

    async def resolve(self, ref: str, *, tenant_id: str) -> str:
        """Decrypt and return the raw value behind one of the tenant's refs."""
        statement = sa.select(SecretRefRow).where(
            SecretRefRow.ref == ref, SecretRefRow.tenant_id == tenant_id
        )
        async with self.sessions() as session:
            row = (await session.scalars(statement)).one_or_none()
        if row is None or row.ciphertext is None:
            raise HarborNotFoundError(f"secret ref not found: {ref!r}")
        try:
            return self._fernet.decrypt(row.ciphertext).decode()
        except InvalidToken as exc:
            logger.error(
                "Secret ref exists but failed to decrypt with the configured key "
                "ref=%r -- this usually means HARBORRAG_SECRETS_ENCRYPTION_KEY was "
                "rotated without re-encrypting stored secrets",
                ref,
            )
            raise HarborSecretDecryptionError(f"secret ref cannot be decrypted: {ref!r}") from exc

    async def delete(self, ref: str, *, tenant_id: str) -> None:
        """Forget the value behind the tenant's ref; a no-op if it's already gone."""
        statement = sa.delete(SecretRefRow).where(
            SecretRefRow.ref == ref, SecretRefRow.tenant_id == tenant_id
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)
