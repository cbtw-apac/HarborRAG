"""Unit tests for the reserved legacy Word and PowerPoint engine configurations."""

from __future__ import annotations

import dataclasses

import pytest

from harborrag_adapters.parsers.document.engines.msword.config import MsWordEngineConfig
from harborrag_adapters.parsers.presentation.engines.msppt.config import (
    MsPowerPointEngineConfig,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("config_type", [MsWordEngineConfig, MsPowerPointEngineConfig])
def test_reserved_legacy_configs_are_empty_frozen_value_objects(config_type: type) -> None:
    config = config_type()

    assert dataclasses.fields(config) == ()
    assert config == config_type()
    assert hash(config) == hash(config_type())
    assert not hasattr(config, "__dict__")
    assert config_type.__slots__ == ()
    assert config_type.__dataclass_params__.frozen is True
