"""Adapter contracts that were stated but not enforced."""

from __future__ import annotations

import re

import pytest

from harborrag_adapters.repositories.errors import (
    MissingOptionalDependencyError,
)
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig

pytestmark = [pytest.mark.unit]


def test_a_missing_extra_is_signalled_by_type_not_by_wording() -> None:
    """The registry matched a regular expression against the message.

    A provider wording it differently, or an ImportError from a transitive
    import, fell through as a bare traceback instead of the configuration
    error naming the extra.
    """

    error = MissingOptionalDependencyError("qdrant-client")

    assert isinstance(error, ImportError)
    assert error.distribution == "qdrant-client"
    # The wording the registry's compatibility path still recognises.
    assert re.fullmatch(r"[A-Za-z0-9_.-]+ is not installed", str(error))


def test_isolation_left_off_still_constructs() -> None:
    config = FalkorDBGraphConfig(
        backend="falkordb",
        instance_name="graph",
        host="127.0.0.1",
    )

    assert config.tenant_isolation is False
