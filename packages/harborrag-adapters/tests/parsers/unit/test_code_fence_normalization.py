"""The fence-line pattern must stay equivalent to the original and run in linear time."""

from __future__ import annotations

import re
import time

import pytest

from harborrag_adapters.parsers.common.normalization import _CODE_FENCE_LINE, strip_code_fences

# The pre-fix pattern: adjacent optional whitespace made it quadratic.
_ORIGINAL = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*[\w+#.-]*[ \t]*$", re.MULTILINE)

_SHAPES = [
    "```",
    "```python",
    "  ~~~ c++  ",
    "```\t\t",
    "``` \t python \t",
    "```py x",
    "   ",
    "",
    "plain text",
    "a b",
    "~~~~ c#",
    "```.net-core",
    "``",
]


@pytest.mark.parametrize("line", _SHAPES)
def test_fence_pattern_matches_the_original_pattern(line: str) -> None:
    text = f"before\n{line}\nafter"
    assert _CODE_FENCE_LINE.sub("", text) == _ORIGINAL.sub("", text)
    assert bool(_CODE_FENCE_LINE.search(line)) == bool(_ORIGINAL.search(line))


def test_strip_code_fences_keeps_fenced_text() -> None:
    assert strip_code_fences("a\n```python\nx = 1\n```\nb") == "a\n\nx = 1\n\nb"


def test_fence_pattern_runs_in_linear_time_on_long_whitespace() -> None:
    hostile = "```" + " " * 400_000 + "!"

    started = time.perf_counter()
    strip_code_fences(hostile)

    assert time.perf_counter() - started < 5.0
