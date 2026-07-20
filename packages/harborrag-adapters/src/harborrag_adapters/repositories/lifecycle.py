from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from harborrag_core.schemas.storage import RepositoryHealth


class AsyncLifecycle(ABC):
    """Defines deterministic ownership for an asynchronous provider resource."""

    @abstractmethod
    async def connect(self) -> None:
        """Open resources owned by this object."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources owned by this object."""

    async def __aenter__(self) -> Self:
        try:
            await self.connect()
        except BaseException:
            # ``__aexit__`` is not called when ``__aenter__`` fails. Providers
            # often allocate a client before their first network operation, so
            # close a partially initialized resource before propagating failure.
            try:
                await self.close()
            except BaseException:
                pass
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()


class RepositoryLifecycle(AsyncLifecycle):
    """Lifecycle contract implemented by every configured repository backend."""

    @abstractmethod
    async def health(self) -> RepositoryHealth:
        """Return a provider-independent and secret-safe health snapshot."""
