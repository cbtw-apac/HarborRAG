from __future__ import annotations

from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def merge_dataclass_options[OptionT: "DataclassInstance"](
    options: OptionT | None,
    option_type: type[OptionT],
    overrides: dict[str, Any],
) -> OptionT:
    """Merge explicit option objects with keyword overrides for backend setup."""
    merged = options or option_type()
    if not overrides:
        return merged
    supported = {field.name for field in fields(merged)}
    unknown = sorted(set(overrides) - supported)
    if unknown:
        names = ", ".join(unknown)
        raise TypeError(f"Unsupported option(s) for {option_type.__name__}: {names}")
    return replace(merged, **overrides)
