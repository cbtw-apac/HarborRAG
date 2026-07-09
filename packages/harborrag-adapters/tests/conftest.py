"""Shared fixtures for the adapters test suite.

Everything external is faked so tests are deterministic and CI-friendly: no
network, no real credentials, no model downloads. Binary builders live in the
importable :mod:`harbor_test_builders` module (this conftest puts the tests
directory on ``sys.path`` so any subdirectory test can import it directly), and
the common ones are re-exported here as fixtures.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from harbor_test_builders import (  # noqa: E402  (path set above)
    build_docx_bytes,
    build_epub_bytes,
    build_png_bytes,
    build_pptx_bytes,
    build_xlsx_bytes,
    build_zip_bomb_bytes,
)


@pytest.fixture
def docx_bytes() -> bytes:
    return build_docx_bytes()


@pytest.fixture
def pptx_bytes() -> bytes:
    return build_pptx_bytes()


@pytest.fixture
def xlsx_bytes() -> bytes:
    return build_xlsx_bytes()


@pytest.fixture
def epub_bytes() -> bytes:
    return build_epub_bytes()


@pytest.fixture
def png_bytes() -> bytes:
    return build_png_bytes()


@pytest.fixture
def zip_bomb_bytes() -> bytes:
    return build_zip_bomb_bytes()
