"""The access file's tenant keys: only a real tenant id is a tenant, never a wildcard."""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from harborrag_runtime.config import (
    ConnectorCatalog,
    ConnectorConfigurationError,
    load_connector_catalog,
)
from harborrag_runtime.config.access_file import load_access_file

# No ``tenants`` allowlist: the connection is open to every tenant, so the catalog check
# alone accepts any tenant key, a wildcard included.
_OPEN_CATALOG = """
version: 1
connectors:
  docs:
    provider: local
"""


@pytest.fixture
def catalog(tmp_path: Path) -> ConnectorCatalog:
    path = tmp_path / "connectors.yaml"
    path.write_text(dedent(_OPEN_CATALOG), encoding="utf-8")
    return load_connector_catalog(path)


def _write(tmp_path: Path, tenant: str) -> Path:
    document = {
        "version": 1,
        "tenants": {
            tenant: {
                "projects": {
                    "beta": {"datasources": ["docs"], "members": [{"principal": "user-a"}]}
                }
            }
        },
    }
    path = tmp_path / "access.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_a_wildcard_tenant_key_is_refused(tmp_path: Path, catalog: ConnectorCatalog) -> None:
    """``*`` would be listed as a tenant, and a wildcard token would keep every tenant."""

    with pytest.raises(ConnectorConfigurationError, match=re.escape("tenants key '*'")):
        load_access_file(_write(tmp_path, "*"), catalog=catalog)


@pytest.mark.parametrize(
    "tenant",
    ["*", "acme*", "-acme", ".acme", "acme corp", "acme/eu", "acme:eu", "a" * 129],
)
def test_a_tenant_key_that_is_not_a_tenant_id_is_refused_by_name(
    tmp_path: Path, catalog: ConnectorCatalog, tenant: str
) -> None:
    with pytest.raises(ConnectorConfigurationError) as refused:
        load_access_file(_write(tmp_path, tenant), catalog=catalog)

    assert f"tenants key {tenant!r} is not a valid tenant id" in str(refused.value)


@pytest.mark.parametrize("tenant", ["acme", "DEFAULT", "tenant_1.eu-west", "a" * 128])
def test_a_tenant_id_is_accepted(tmp_path: Path, catalog: ConnectorCatalog, tenant: str) -> None:
    loaded = load_access_file(_write(tmp_path, tenant), catalog=catalog)

    assert loaded.entitled_connections(tenant, "user-a") == frozenset({"docs"})
