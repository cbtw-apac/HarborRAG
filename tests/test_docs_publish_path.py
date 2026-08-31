"""Tests for the documentation publication-boundary classifier."""

import pytest
from website.check_docs_publish_path import (
    INTERNAL,
    PACKAGE_DETAIL,
    PUBLISHED,
    classify,
    group,
    main,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture
def package_root(tmp_path):
    """A workspace holding one real distribution and one plain directory."""
    package = tmp_path / "packages" / "harborrag-core"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "packages" / "not-a-distribution").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "docs/TOC.md",
        "docs/users/configuration/model-config.md",
        "packages/harborrag-core/README.md",
    ],
)
def test_published_sources_are_recognised(path, package_root):
    assert classify(path, package_root) == PUBLISHED


@pytest.mark.parametrize(
    "path",
    [
        "packages/harborrag-core/src/harborrag_core/ports/README.md",
        "packages/harborrag-core/tests/README.md",
        "deploy/aws/README.md",
        "website/README.md",
        "examples/README.md",
    ],
)
def test_in_tree_notes_are_recognised(path, package_root):
    assert classify(path, package_root) == INTERNAL


def test_package_detail_docs_are_their_own_category(package_root):
    assert classify("packages/harborrag-core/docs/retrieval.md", package_root) == PACKAGE_DETAIL


def test_a_readme_without_a_sibling_pyproject_is_not_published(package_root):
    # packages/<name>/README.md only reaches the site when the directory is a
    # real distribution; the builder discovers packages by their pyproject.
    assert classify("packages/not-a-distribution/README.md", package_root) == INTERNAL


def test_group_buckets_every_path(package_root):
    grouped = group(
        ["docs/TOC.md", "packages/harborrag-core/docs/a.md", "website/README.md"],
        package_root,
    )

    assert grouped[PUBLISHED] == ["docs/TOC.md"]
    assert grouped[PACKAGE_DETAIL] == ["packages/harborrag-core/docs/a.md"]
    assert grouped[INTERNAL] == ["website/README.md"]


def test_default_report_is_advisory(capsys):
    assert main([]) == 0
    assert "Documentation publication boundary" in capsys.readouterr().out


def test_strict_mode_fails_on_an_in_tree_note(capsys):
    assert main(["--strict", "website/README.md"]) == 1
    assert "never reaches the website" in capsys.readouterr().out


def test_strict_mode_passes_for_published_sources():
    assert main(["--strict", "docs/TOC.md"]) == 0
