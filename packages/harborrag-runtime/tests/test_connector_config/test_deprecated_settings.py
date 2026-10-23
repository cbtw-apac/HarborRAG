"""A setting removed after 2.0.0a1 still loads, ignored with a WARNING.

The 2.0.0a1 ``config/connectors.yaml`` set ``max_child_pages`` on
``confluence-main``. Rejecting it as an unknown setting turned that connector
into an invalid definition on upgrade, so no Confluence ingestion ran until
someone edited the file by hand.
"""

from __future__ import annotations

import logging

from harborrag_runtime.config import connector_fingerprint, load_connector_catalog

from .conftest import write_config

_ALPHA1_CONFLUENCE = """
version: 1
connectors:
  confluence-main:
    provider: confluence
    environment:
      base_url: CONFLUENCE_BASE_URL
      space_key: CONFLUENCE_SPACE_KEY
    settings:
      deployment_type: cloud
      content_types: [page]
      max_child_pages: 1
    secrets:
      token_env: CONFLUENCE_TOKEN
      email_env: CONFLUENCE_EMAIL
"""


def test_an_alpha1_max_child_pages_setting_is_ignored_with_a_warning(tmp_path, caplog) -> None:
    path = write_config(tmp_path, _ALPHA1_CONFLUENCE)

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.config.connectors"):
        catalog = load_connector_catalog(path)
        definition = catalog.get("confluence-main")

    assert "max_child_pages" not in definition.settings
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any(
        "confluence-main" in record.getMessage() and "max_child_pages" in record.getMessage()
        for record in warnings
    )


def test_ignoring_it_fingerprints_like_the_edited_file(tmp_path) -> None:
    (tmp_path / "legacy").mkdir()
    (tmp_path / "edited").mkdir()
    legacy = load_connector_catalog(write_config(tmp_path / "legacy", _ALPHA1_CONFLUENCE))
    edited = load_connector_catalog(
        write_config(
            tmp_path / "edited",
            _ALPHA1_CONFLUENCE.replace("      max_child_pages: 1\n", ""),
        )
    )

    def fingerprint(catalog) -> str:  # type: ignore[no-untyped-def]
        return connector_fingerprint(
            catalog_version=1,
            definition=catalog.get("confluence-main"),
            environment={
                "CONFLUENCE_BASE_URL": "https://example.atlassian.net",
                "CONFLUENCE_SPACE_KEY": "ENG",
            },
        )

    assert fingerprint(legacy) == fingerprint(edited)


def test_an_environment_mapped_max_child_pages_is_ignored_too(tmp_path) -> None:
    content = _ALPHA1_CONFLUENCE.replace("      max_child_pages: 1\n", "").replace(
        "      space_key: CONFLUENCE_SPACE_KEY\n",
        "      space_key: CONFLUENCE_SPACE_KEY\n      max_child_pages: CONFLUENCE_MAX_CHILD_PAGES\n",
    )

    definition = load_connector_catalog(write_config(tmp_path, content)).get("confluence-main")

    assert "max_child_pages" not in definition.setting_environment
