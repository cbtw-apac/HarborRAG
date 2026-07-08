from __future__ import annotations

from .base import BaseConnector
from .exceptions import ConnectorNotFoundError


class ConnectorRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, type[BaseConnector]] = {}

    def register(
        self, name: str, provider_cls: type[BaseConnector], *, aliases: list[str] | None = None
    ) -> None:
        self._providers[name] = provider_cls
        for alias in aliases or []:
            self._providers[alias] = provider_cls

    def create(self, name: str, **kwargs) -> BaseConnector:
        try:
            cls = self._providers[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(f"Unsupported connector provider: {name}") from exc
        return cls(**kwargs)

    def names(self) -> list[str]:
        return sorted(self._providers)


connector_registry = ConnectorRegistry()
