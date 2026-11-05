"""Language names for code refinement: normalisation, guessing and grammars."""

from __future__ import annotations

from pathlib import PurePosixPath

_ALIASES = {
    "py": "python",
    "python3": "python",
    "py3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
    "mts": "typescript",
    "cts": "typescript",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "hh": "cpp",
    "h": "c",
    "cs": "csharp",
    "c#": "csharp",
    "c-sharp": "csharp",
    "golang": "go",
    "rs": "rust",
    "rb": "ruby",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "console": "bash",
    "yml": "yaml",
    "kt": "kotlin",
    "kts": "kotlin",
}

_EXTENSIONS = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "bash",
    ".bash": "bash",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".swift": "swift",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
}

# language -> (grammar module, factory attribute). Grammars ship inside these
# wheels (``harborrag-adapters[chunking-code]``), so loading never downloads.
GRAMMARS: dict[str, tuple[str, str]] = {
    "bash": ("tree_sitter_bash", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "php": ("tree_sitter_php", "language_php"),
    "python": ("tree_sitter_python", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
}


# The grammar minors pinned by the ``chunking-code`` extra. Split boundaries
# depend on them, so this string is part of the code refiner's identity; update
# it together with the pins in pyproject.toml. It is declared rather than read
# from the environment so every process (API, worker) computes the same
# processing identity whether or not the extra is installed.
GRAMMAR_SET = (
    "tree-sitter-0.25;bash-0.25;c-0.24;c-sharp-0.23;cpp-0.23;go-0.25;java-0.23;"
    "javascript-0.25;php-0.25;python-0.25;ruby-0.23;rust-0.24;typescript-0.23"
)


def normalize_language(value: str | None) -> str | None:
    """Return the canonical lowercase language name, or ``None`` when unknown/blank."""

    if value is None:
        return None
    name = value.strip().lower()
    if not name:
        return None
    return _ALIASES.get(name, name)


def guess_language(*, filename: str | None = None, fence_info: str | None = None) -> str | None:
    """Guess a language from a Markdown fence info string or a file name."""

    if fence_info and fence_info.strip():
        word = fence_info.strip().split()[0].strip("{}.").split(",")[0]
        language = normalize_language(word)
        if language:
            return language
    if filename and filename.strip():
        suffix = PurePosixPath(filename.strip().replace("\\", "/")).suffix.lower()
        return _EXTENSIONS.get(suffix)
    return None
