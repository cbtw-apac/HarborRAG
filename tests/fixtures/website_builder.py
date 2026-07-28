"""Fixture plugin exposing the website builder to the builder-module tests."""

import importlib.util
from pathlib import Path

import pytest

BUILD_FILE = Path(__file__).resolve().parents[2] / "website" / "build.py"


@pytest.fixture(scope="session")
def website_build_module():
    """Load website/build.py once per session."""
    if not BUILD_FILE.exists():
        pytest.skip("Website build.py not found")

    spec = importlib.util.spec_from_file_location("build", BUILD_FILE)
    if spec is None or spec.loader is None:
        pytest.skip("Cannot load build module")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def website_builder_cls(website_build_module):
    """The WebsiteBuilder class under test."""
    return website_build_module.WebsiteBuilder


@pytest.fixture
def build_main(website_build_module):
    """The argparse entry point from website/build.py."""
    return website_build_module.main


@pytest.fixture
def builder(mock_project_structure, monkeypatch, website_builder_cls):
    """A WebsiteBuilder rooted in the mock project workspace."""
    monkeypatch.chdir(mock_project_structure)
    return website_builder_cls("website/templates", "site")


@pytest.fixture
def markdown_processor():
    """A standalone MarkdownProcessor (website/ is on sys.path via conftest)."""
    from builder.markdown import MarkdownProcessor

    return MarkdownProcessor()
