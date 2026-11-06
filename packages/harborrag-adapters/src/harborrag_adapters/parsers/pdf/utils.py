"""Helpers shared by the PDF engines: option merging and the PyMuPDF import."""

from __future__ import annotations

import importlib
from dataclasses import fields, replace
from types import ModuleType
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


def import_pymupdf() -> ModuleType:
    """Import PyMuPDF under its modern ``pymupdf`` name or its legacy ``fitz`` name.

    Returns:
        The imported PyMuPDF module.

    Raises:
        ImportError: Neither name is installed; the message names the extra to add.
    """
    try:
        return importlib.import_module("pymupdf")
    except ImportError:
        try:
            return importlib.import_module("fitz")
        except ImportError as exc:
            raise ImportError(
                "PDF parsing with PyMuPDF requires `PyMuPDF`; install "
                "`harborrag-adapters[pdf-pymupdf]` or `pip install PyMuPDF`."
            ) from exc
