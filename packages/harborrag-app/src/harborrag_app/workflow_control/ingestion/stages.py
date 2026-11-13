"""Map the pipeline's internal stage names onto the spec's five failure stages."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

DEFAULT_SPEC_STAGE = "capture"
SPEC_STAGES: Mapping[str, str] = MappingProxyType(
    {
        "FetchAndCaptureRaw": "capture",
        "ParseAndNormalize": "interpret",
        "ChunkAndValidate": "interpret",
        "PersistCanonical": "commit",
        "SyncContentUnits": "commit",
        "EncodeChunks": "derive",
        "BuildProjections": "derive",
        "WriteVectorProjection": "derive",
        "WriteGraphProjection": "derive",
        "BuildRelations": "derive",
        "VerifyProjections": "publish",
        "PublishVersion": "publish",
    }
)


def spec_stage(internal: str | None) -> str:
    """Return the spec stage of an internal stage; unknown or missing is ``capture``."""
    return SPEC_STAGES.get(internal or "", DEFAULT_SPEC_STAGE)


def internal_stages(spec: str) -> frozenset[str]:
    """Return the internal stages a spec stage covers; empty for an unknown spec stage."""
    return frozenset(internal for internal, mapped in SPEC_STAGES.items() if mapped == spec)
