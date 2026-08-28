"""Tests for the public documentation publication boundary."""

from website.check_publication import (
    find_generated_reference_leaks,
    find_guard_scope_failures,
    find_public_reference_leaks,
    public_candidate_files,
    publication_failures,
)


def test_public_sources_do_not_reference_private_inputs(project_root_dir):
    assert find_public_reference_leaks(project_root_dir) == []


def test_generated_site_leak_is_detected(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        '<a href="HARBORRAG_ARCHITECTURE.md">internal</a>', encoding="utf-8"
    )

    failures = find_generated_reference_leaks(tmp_path)

    assert failures == ["site/index.html:1: HARBORRAG_ARCHITECTURE.md"]


def test_guard_examines_a_non_empty_candidate_set(project_root_dir):
    assert public_candidate_files(project_root_dir)


def test_guard_scope_is_satisfied_by_this_repository(project_root_dir):
    assert find_guard_scope_failures(project_root_dir) == []


def test_guard_fails_loudly_when_the_documentation_tree_is_absent(tmp_path):
    # A layout with no docs/ and no website/templates - the shape the guard
    # would silently pass in a documentation-only repository.
    failures = find_guard_scope_failures(tmp_path)

    assert failures, "an empty scope must be reported, not treated as a clean run"
    assert any("no documentation tree" in failure for failure in failures)
    assert find_public_reference_leaks(tmp_path) == []
    assert publication_failures(tmp_path), "publication_failures must surface scope failures"
