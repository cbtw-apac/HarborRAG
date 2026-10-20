"""Machine codes for items a connector skipped on purpose."""

from __future__ import annotations

from enum import StrEnum


class SkipReason(StrEnum):
    """Why an item was excluded; the ``reason`` of a ``skipped`` failure."""

    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    EMPTY = "empty"
    OUT_OF_SCOPE = "out_of_scope"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"
    LFS_POINTER = "lfs_pointer"
    TOO_MANY_CHILDREN = "too_many_children"
