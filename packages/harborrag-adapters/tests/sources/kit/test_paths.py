from __future__ import annotations

from harborrag_adapters.sources._kit.paths import PathRules, is_within


def test_double_star_matches_root_level() -> None:
    assert not PathRules.of(exclude=["**/secret.txt"]).admits("secret.txt")


def test_single_star_does_not_cross_slash() -> None:
    rules = PathRules.of(include=["docs/*.md"])
    assert rules.admits("docs/a.md")
    assert not rules.admits("docs/a/b.md")


def test_unanchored_pattern_matches_any_depth() -> None:
    assert not PathRules.of(exclude=["*.log"]).admits("a/b/c.log")


def test_directory_pattern_covers_children() -> None:
    assert not PathRules.of(exclude=["node_modules/"]).admits("web/node_modules/x/index.js")


def test_exclude_wins_over_include() -> None:
    rules = PathRules.of(include=["src/**"], exclude=["src/gen/**"])
    assert rules.admits("src/a.py")
    assert not rules.admits("src/gen/a.py")


def test_empty_rules_admit_everything() -> None:
    assert PathRules.of().admits("any/thing.bin")


def test_reported_path_is_not_rewritten() -> None:
    # A backslash in a reported path is a name character, not a separator.
    assert PathRules.of(include=["*.txt"]).admits("a\\b.txt")
    assert PathRules.of(exclude=["a/b.txt"]).admits("a\\b.txt")


def test_patterns_are_nfc_normalised() -> None:
    nfd, nfc = "cafe\u0301.md", "caf\u00e9.md"
    assert PathRules.of(include=[nfd]).admits(nfc)
    assert PathRules.of(include=[nfc]).admits(nfd)


def test_is_within() -> None:
    assert is_within("docs/a.md", "docs")
    assert not is_within("docsx/a.md", "docs")
    assert is_within("docs", "docs")
    assert is_within("x", "")
