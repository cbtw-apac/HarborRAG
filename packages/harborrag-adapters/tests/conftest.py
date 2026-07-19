from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from harbor_test_builders import (  # noqa: E402  (path set above)
    build_docx_bytes,
    build_zip_bomb_bytes,
)


@pytest.fixture
def docx_bytes() -> bytes:
    return build_docx_bytes()


@pytest.fixture
def zip_bomb_bytes() -> bytes:
    return build_zip_bomb_bytes()
