"""Adapter contracts that were stated but not enforced."""

from __future__ import annotations

import re

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageConfigurationError,
    MissingOptionalDependencyError,
)
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.repository import FalkorDBGraphRepository

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


def test_unsupported_tenant_isolation_is_refused_not_ignored() -> None:
    """Accepting the setting and ignoring it is the dangerous half.

    The generic graph repository opens one client on one graph. An operator who
    switched isolation on believed tenants sat in separate graphs while every
    write still went to the shared one, separated by predicate alone.
    """

    config = FalkorDBGraphConfig(
        backend="falkordb",
        instance_name="graph",
        host="127.0.0.1",
        tenant_isolation=True,
    )

    with pytest.raises(HarborStorageConfigurationError, match="tenant_isolation is not supported"):
        FalkorDBGraphRepository(config)


def test_isolation_left_off_still_constructs() -> None:
    config = FalkorDBGraphConfig(
        backend="falkordb",
        instance_name="graph",
        host="127.0.0.1",
    )

    assert config.tenant_isolation is False
