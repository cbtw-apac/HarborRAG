from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Annotated, Any

from pydantic import AfterValidator, PlainSerializer


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMetadata(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value


class FrozenMetadata(Mapping[str, Any]):
    """Recursively immutable provider metadata with JSON-compatible serialization."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = {
            str(key): _freeze_metadata_value(value) for key, value in (values or {}).items()
        }

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenMetadata({self._values!r})"


def thaw_metadata(value: Any) -> Any:
    """Return ordinary JSON containers from recursively frozen metadata."""

    if isinstance(value, Mapping):
        return {str(key): thaw_metadata(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_metadata(item) for item in value]
    return value


ChunkMetadata = Annotated[
    Mapping[str, Any],
    AfterValidator(FrozenMetadata),
    PlainSerializer(thaw_metadata, return_type=dict[str, Any]),
]
