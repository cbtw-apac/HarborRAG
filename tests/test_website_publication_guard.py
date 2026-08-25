"""Tests for the public documentation publication boundary."""

from website.check_publication import (
    find_generated_reference_leaks,
    find_public_reference_leaks,
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
