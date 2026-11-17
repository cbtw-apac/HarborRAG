"""The internal-to-spec failure stage table."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.ingestion.stages import SPEC_STAGES, internal_stages, spec_stage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("internal", "expected"),
    [
        ("FetchAndCaptureRaw", "capture"),
        ("ParseAndNormalize", "interpret"),
        ("ChunkAndValidate", "interpret"),
        ("PersistCanonical", "commit"),
        ("SyncContentUnits", "commit"),
        ("EncodeChunks", "derive"),
        ("BuildProjections", "derive"),
        ("WriteVectorProjection", "derive"),
        ("WriteGraphProjection", "derive"),
        ("BuildRelations", "derive"),
        ("VerifyProjections", "publish"),
        ("PublishVersion", "publish"),
        ("Unheard", "capture"),
        (None, "capture"),
    ],
)
def test_spec_stage_maps_each_internal_stage(internal: str | None, expected: str) -> None:
    assert spec_stage(internal) == expected


def test_internal_stages_inverts_the_table() -> None:
    assert internal_stages("interpret") == {"ParseAndNormalize", "ChunkAndValidate"}
    assert internal_stages("publish") == {"VerifyProjections", "PublishVersion"}
    assert internal_stages("nope") == frozenset()
    covered = set().union(*(internal_stages(s) for s in set(SPEC_STAGES.values())))
    assert covered == set(SPEC_STAGES)
