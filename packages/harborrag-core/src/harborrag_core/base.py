from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class StrictModel(BaseModel):
    """Recursively immutable model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def model_post_init(self, context: Any, /) -> None:
        del context
        for name, value in self.__dict__.items():
            object.__setattr__(self, name, _deep_freeze(value))
        if self.__pydantic_extra__ is not None:
            object.__setattr__(self, "__pydantic_extra__", _deep_freeze(self.__pydantic_extra__))


class ExtensibleModel(BaseModel):
    """Recursively immutable model that preserves provider-specific fields."""

    model_config = ConfigDict(extra="allow", frozen=True)

    def model_post_init(self, context: Any, /) -> None:
        del context
        for name, value in self.__dict__.items():
            object.__setattr__(self, name, _deep_freeze(value))
        if self.__pydantic_extra__ is not None:
            object.__setattr__(self, "__pydantic_extra__", _deep_freeze(self.__pydantic_extra__))


class FrozenDict(dict[Any, Any]):
    """A ``dict`` compatible with Pydantic serializers but closed to mutation."""

    def _immutable(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("frozen model containers are immutable")

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        memo[id(self)] = self
        return self

    __delitem__ = _immutable
    __ior__ = _immutable  # type: ignore[assignment]
    __setitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable  # type: ignore[assignment]
    setdefault = _immutable
    update = _immutable


class FrozenList(list[Any]):
    """A ``list`` compatible with Pydantic serializers but closed to mutation."""

    def _immutable(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("frozen model containers are immutable")

    def __copy__(self) -> FrozenList:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenList:
        memo[id(self)] = self
        return self

    __delitem__ = _immutable
    __iadd__ = _immutable  # type: ignore[assignment]
    __imul__ = _immutable  # type: ignore[assignment]
    __setitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _deep_freeze(value: Any) -> Any:
    """Freeze nested built-in containers while retaining JSON serialization."""

    if isinstance(value, BaseModel):
        return value
    if (
        type(value).__name__ == "FrozenMetadata"
        and type(value).__module__ == "harborrag_core.chunking.metadata"
    ):
        return value
    if isinstance(value, Mapping):
        return FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value
