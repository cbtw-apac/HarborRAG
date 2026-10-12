from __future__ import annotations

import pytest

from harborrag_adapters.chunking.code_structure import (
    CodeStructureRefiner,
    default_parser_loader,
)
from harborrag_adapters.chunking.languages import GRAMMAR_SET, guess_language, normalize_language
from harborrag_core.contracts.chunking import SplitBoundaryKind, TextRefinementRequest

from .refiner_contract import CharacterCounter, assert_valid_splits, reconstruct

PYTHON_SOURCE = '''import os


class Store:
    """Keeps values."""

    def get(self, key):
        value = self._values.get(key)
        if value is None:
            raise KeyError(key)
        return value

    # Writes are validated first.
    @validated
    def put(self, key, value):
        self._values[key] = value
        self._log.append(key)
        return value


def helper(x):
    return x * 2
'''

TYPESCRIPT_SOURCE = """export class Store {
  get(key: string): number {
    const value = this.values.get(key);
    return value ?? 0;
  }

  put(key: string, value: number): void {
    this.values.set(key, value);
    this.log.push(key);
  }
}

export function helper(x: number): number {
  return x * 2;
}
"""


def code_request(content: str, maximum: int, language: str | None) -> TextRefinementRequest:
    return TextRefinementRequest(
        content=content,
        maximum_tokens=maximum,
        boundary_kind=SplitBoundaryKind.CODE_BLOCK,
        language=language,
    )


def split_code(refiner: CodeStructureRefiner, request: TextRefinementRequest):
    splits = refiner.split(request)
    assert_valid_splits(request, splits, CharacterCounter())
    assert reconstruct(request, splits) == request.content
    return splits


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Py", "python"),
        ("ts", "typescript"),
        ("C++", "cpp"),
        ("c#", "csharp"),
        (" ", None),
        (None, None),
    ],
)
def test_language_normalisation(value: str | None, expected: str | None) -> None:
    assert normalize_language(value) == expected


def test_language_guessing_prefers_the_fence_then_the_filename() -> None:
    assert guess_language(fence_info="python title=x.py") == "python"
    assert guess_language(fence_info="{.ts}") == "typescript"
    assert guess_language(filename="src/app/main.RS") == "rust"
    assert guess_language(filename="README") is None


def test_separator_tier_cuts_at_definitions_without_a_parser() -> None:
    refiner = CodeStructureRefiner(CharacterCounter(), parser_loader=lambda language: None)

    splits = split_code(refiner, code_request(PYTHON_SOURCE, 200, "python"))

    starts = [split.content.lstrip().split("\n", 1)[0] for split in splits]
    assert any(start.startswith("def helper") for start in starts)
    assert all(split.content.endswith("\n") for split in splits)


def test_unknown_language_still_splits_at_lines() -> None:
    content = "".join(f"step {index} does a thing\n" for index in range(20))
    refiner = CodeStructureRefiner(CharacterCounter())

    splits = split_code(refiner, code_request(content, 60, "cobol"))

    assert all(split.content.endswith("\n") for split in splits)


def test_a_single_overlong_line_is_force_windowed() -> None:
    content = "x = '" + "a" * 300 + "'\n"
    refiner = CodeStructureRefiner(CharacterCounter(), parser_loader=lambda language: None)

    splits = split_code(refiner, code_request(content, 100, "python"))

    assert all(split.token_count <= 100 for split in splits)


def test_fingerprint_names_the_grammars() -> None:
    fingerprint = CodeStructureRefiner(CharacterCounter()).fingerprint()

    assert fingerprint.startswith("code-structure:1[")
    assert GRAMMAR_SET in fingerprint


class TestSyntaxTier:
    @pytest.fixture(autouse=True)
    def _grammars(self) -> None:
        pytest.importorskip("tree_sitter")
        pytest.importorskip("tree_sitter_python")
        pytest.importorskip("tree_sitter_typescript")

    def test_python_class_splits_at_methods_with_class_scope(self) -> None:
        refiner = CodeStructureRefiner(CharacterCounter())

        splits = split_code(refiner, code_request(PYTHON_SOURCE, 200, "python"))

        by_start = {split.content.lstrip().split("\n", 1)[0]: split for split in splits}
        put = next(split for start, split in by_start.items() if start.startswith("# Writes"))
        assert "@validated\n    def put" in put.content
        assert put.prefix == "class Store:"
        assert any(start.startswith("def helper") for start in by_start)

    def test_method_header_stays_with_its_body(self) -> None:
        refiner = CodeStructureRefiner(CharacterCounter())

        splits = split_code(refiner, code_request(PYTHON_SOURCE, 120, "python"))

        for split in splits:
            assert not split.content.rstrip().endswith(":"), split.content

    def test_statements_of_an_oversized_method_carry_the_method_scope(self) -> None:
        body = "".join(f"    total += compute_value_{index}(item)\n" for index in range(12))
        content = f"class Totals:\n    def run(self, item):\n        total = 0\n{body.replace('    total', '        total')}        return total\n"
        refiner = CodeStructureRefiner(CharacterCounter())

        splits = split_code(refiner, code_request(content, 160, "python"))

        assert len(splits) > 1
        assert splits[-1].prefix == "class Totals:\ndef run(self, item):"

    def test_typescript_class_splits_at_members(self) -> None:
        refiner = CodeStructureRefiner(CharacterCounter())

        splits = split_code(refiner, code_request(TYPESCRIPT_SOURCE, 150, "typescript"))

        assert any(split.content.lstrip().startswith("put(") for split in splits)
        put = next(split for split in splits if split.content.lstrip().startswith("put("))
        assert put.prefix == "export class Store {"

    def test_non_ascii_source_maps_byte_offsets_to_characters(self) -> None:
        content = PYTHON_SOURCE.replace("Keeps values.", "Giữ các giá trị — 値を保持する.")
        refiner = CodeStructureRefiner(CharacterCounter())

        split_code(refiner, code_request(content, 200, "python"))

    def test_syntax_errors_fall_back_to_separators(self) -> None:
        broken = PYTHON_SOURCE.replace("def put(self, key, value):", "def put(self, key, value:")
        refiner = CodeStructureRefiner(CharacterCounter())

        split_code(refiner, code_request(broken, 200, "python"))

    def test_crlf_source_is_tiled_exactly(self) -> None:
        refiner = CodeStructureRefiner(CharacterCounter())

        split_code(refiner, code_request(PYTHON_SOURCE.replace("\n", "\r\n"), 200, "python"))

    def test_default_loader_builds_known_grammars_only(self) -> None:
        assert default_parser_loader("python") is not None
        assert default_parser_loader("cobol") is None

    def test_output_is_deterministic(self) -> None:
        request = code_request(PYTHON_SOURCE, 150, "python")

        assert CodeStructureRefiner(CharacterCounter()).split(request) == CodeStructureRefiner(
            CharacterCounter()
        ).split(request)
