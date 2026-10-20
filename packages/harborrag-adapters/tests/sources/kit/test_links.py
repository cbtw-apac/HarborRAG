from __future__ import annotations

import random
import time

from harborrag_adapters.sources._kit.render import Link, extract_links


def test_extract_links_inline_image_autolink() -> None:
    md = "See [docs](a/b.md) and ![logo](img/l.png) or <https://x.example/y>."
    assert [link.target for link in extract_links(md)] == [
        "a/b.md",
        "img/l.png",
        "https://x.example/y",
    ]
    assert extract_links("![logo](l.png)") == (Link("l.png", "logo", True),)


def test_extract_links_keeps_balanced_parentheses() -> None:
    assert extract_links("[Foo](https://wiki/Foo_(bar))")[0].target == "https://wiki/Foo_(bar)"


def test_extract_links_ignores_code() -> None:
    md = "`[a](x)`\n\n```\n[b](y)\n```\n[c](z)"
    assert extract_links(md) == (Link("z", "c", False),)


def test_extract_links_ignores_tilde_fence() -> None:
    assert extract_links("~~~\n[b](y)\n~~~\n[c](z)") == (Link("z", "c", False),)


def test_extract_links_dedupes_by_target() -> None:
    assert extract_links("[a](x) [b](x) [c](y)") == (Link("x", "a", False), Link("y", "c", False))


def test_extract_links_drops_title_and_empty_targets() -> None:
    assert extract_links('[a](x "T") [b]() [c](<p q>)') == (
        Link("x", "a", False),
        Link("p q", "c", False),
    )


def test_extract_links_image_inside_link() -> None:
    assert [link.target for link in extract_links("[![a](i.png)](page)")] == ["page", "i.png"]


def test_extract_links_is_linear_on_hostile_input() -> None:
    hostile_inputs = (
        "[](" * 50_000,
        "[" * 150_000,
        "`" * 150_000,
        "<a:/" * 40_000,
        "`\r\n \r\n" * 30_000,
        "[a](b \n" * 30_000,
    )
    for hostile in hostile_inputs:
        started = time.perf_counter()
        assert extract_links(hostile) == ()
        assert time.perf_counter() - started < 2.0


def test_extract_links_caps_count() -> None:
    md = " ".join(f"[a](x{i})" for i in range(5000))
    assert len(extract_links(md, limit=10)) == 10


def test_extract_links_code_span_does_not_cross_paragraphs() -> None:
    md = "don`t do it [a](1)\n\npara [b](2)\n\nit`s fine [c](3)"
    assert [link.target for link in extract_links(md)] == ["1", "2", "3"]


def test_extract_links_backtick_fence_with_inline_code_is_not_a_fence() -> None:
    md = "```code``` then [a](1)\n\n[b](2)"
    assert [link.target for link in extract_links(md)] == ["1", "2"]


def test_extract_links_escaped_backtick_starting_a_run() -> None:
    assert extract_links("\\``x") == ()
    assert [link.target for link in extract_links("a \\`` b [l](t)")] == ["t"]


def test_extract_links_code_span_may_close_after_a_backslash() -> None:
    # Backslash escapes do not apply inside a code span.
    assert [link.target for link in extract_links("`a\\` [l](t)")] == ["t"]


def test_extract_links_never_raises_on_random_input() -> None:
    alphabet = "`\\[]()<>!\n\r \t~:/ax"
    rng = random.Random(20261020)
    for _ in range(5000):
        sample = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        extract_links(sample)


def test_extract_links_code_span_does_not_cross_crlf_paragraphs() -> None:
    md = "don`t [a](1)\r\n\r\npara [b](2)\r\n\r\nit`s [c](3)"
    assert [link.target for link in extract_links(md)] == ["1", "2", "3"]


def test_extract_links_code_span_does_not_cross_whitespace_blank_lines() -> None:
    for blank in ("\n \n", "\n\t\n"):
        md = f"don`t [a](1){blank}para [b](2){blank}it`s [c](3)"
        assert [link.target for link in extract_links(md)] == ["1", "2", "3"]


def test_extract_links_title_does_not_cross_whitespace_blank_lines() -> None:
    assert extract_links("[a](x \n \n y)") == ()


def test_extract_links_escaped_image_marker_is_not_an_image() -> None:
    assert extract_links("\\![a](b)")[0].image is False
    assert extract_links("\\\\![a](b)")[0].image is True
