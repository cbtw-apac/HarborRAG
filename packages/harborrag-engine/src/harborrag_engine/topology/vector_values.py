"""Canonical storage precision for frozen derived-vector artifacts."""

from __future__ import annotations

import math
import struct


def canonical_dense_vector(vector: tuple[float, ...]) -> tuple[float, ...]:
    """Round once to Qdrant's float32 precision before hashing/persisting artifacts.

    This does not normalize vectors. Cosine indexes may normalize on upload;
    callers must choose their normalization and comparison contract separately.
    """
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("dense vector must contain finite values")
    format_code = f"!{len(vector)}f"
    try:
        packed = struct.pack(format_code, *vector)
    except (OverflowError, struct.error) as error:
        raise ValueError("dense vector exceeds float32 representation") from error
    canonical = tuple(float(value) for value in struct.unpack(format_code, packed))
    if not all(math.isfinite(value) for value in canonical):
        raise ValueError("dense vector exceeds finite float32 representation")
    return canonical
