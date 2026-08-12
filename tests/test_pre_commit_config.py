"""Contract tests binding .pre-commit-config.yaml to the CI quality workflow.

A hook that silently disagrees with CI is worse than no hook, because it buys
false confidence. These tests fail when the two definitions drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.workflow]

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "quality-gates.yml"

# Every `uv run make <target>` gate in quality-gates.yml maps to the hook ids
# that reproduce it locally. Add an entry here when you add a CI gate.
MAKE_TARGET_TO_HOOK_IDS: dict[str, tuple[str, ...]] = {
    "lint": ("ruff-format", "ruff-check"),
    "complexity": ("complexity-ratchet",),
    "file-length": ("file-length",),
    "import-boundaries": ("import-boundaries",),
    "typecheck": ("typecheck",),
    "deps-check": ("deps-check",),
    "compile": ("compile",),
}

# CI gates deliberately left out of the hooks, each with a stated reason.
CI_ONLY_TARGETS: dict[str, str] = {
    "coverage": (
        "Fifteen tests fail on Windows: five need symlink privileges and the "
        "rest hit the unguarded os.fchmod calls in harborrag-mcp-server. A "
        "local pytest gate would block every push from a Windows machine. "
        "Restore the hook once those failures are fixed."
    ),
}

# Hooks that inspect the whole repository rather than the staged paths. Passing
# filenames to these would silently narrow or corrupt their input.
REPO_WIDE_HOOK_IDS = frozenset(
    {
        "file-length",
        "complexity-ratchet",
        "import-boundaries",
        "deps-check",
        "compile",
        "typecheck",
    }
)

# Gates too slow for every commit; they belong on push.
PRE_PUSH_HOOK_IDS = frozenset(
    {
        "import-boundaries",
        "deps-check",
        "compile",
        "typecheck",
    }
)

MAKE_STEP = re.compile(r"^uv run make ([\w-]+)$")


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def local_hooks() -> dict[str, dict[str, Any]]:
    """Return the repo-local hooks keyed by id."""

    config = load_yaml(CONFIG_PATH)
    for repository in config["repos"]:
        if repository["repo"] == "local":
            return {hook["id"]: hook for hook in repository["hooks"]}
    raise AssertionError("no local repo declared in .pre-commit-config.yaml")


def workflow_make_targets() -> set[str]:
    """Return the make targets invoked by the quality-gates workflow."""

    workflow = load_yaml(WORKFLOW_PATH)
    targets: set[str] = set()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            match = MAKE_STEP.match(str(step.get("run", "")).strip())
            if match:
                targets.add(match.group(1))
    return targets


def test_every_ci_gate_has_a_hook_or_a_stated_exemption() -> None:
    unmapped = workflow_make_targets() - set(MAKE_TARGET_TO_HOOK_IDS) - set(CI_ONLY_TARGETS)

    assert not unmapped, (
        f"CI gates with no local hook and no stated exemption: {sorted(unmapped)}. "
        "Add a hook to .pre-commit-config.yaml, or record the reason in CI_ONLY_TARGETS."
    )


def test_mapped_hooks_all_exist() -> None:
    hooks = local_hooks()
    expected = {hook_id for ids in MAKE_TARGET_TO_HOOK_IDS.values() for hook_id in ids}

    assert expected <= set(hooks), (
        f"hook ids missing from the config: {sorted(expected - set(hooks))}"
    )


def test_repo_wide_hooks_do_not_receive_filenames() -> None:
    for hook_id, hook in local_hooks().items():
        if hook_id not in REPO_WIDE_HOOK_IDS:
            continue
        assert hook.get("pass_filenames") is False, (
            f"{hook_id} scans the whole repository; pass_filenames must be false "
            "or pre-commit will append staged paths and narrow its input"
        )


def test_expensive_hooks_run_on_push_not_on_commit() -> None:
    for hook_id, hook in local_hooks().items():
        stages = hook.get("stages")
        if hook_id in PRE_PUSH_HOOK_IDS:
            assert stages == ["pre-push"], f"{hook_id} must run on push only, got {stages!r}"
        else:
            assert stages is None, f"{hook_id} should run on every commit, got {stages!r}"


def test_ruff_hooks_match_the_makefile_flags() -> None:
    """The ratcheted rules are excluded in Make; the hook must agree."""

    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    hooks = local_hooks()

    assert "--ignore C901,PLR0913" in hooks["ruff-check"]["entry"]
    assert "--ignore C901,PLR0913" in makefile
    assert "--fix" in hooks["ruff-check"]["entry"], "the commit hook fixes, then fails"


def test_hooks_resolve_tools_through_the_lockfile() -> None:
    """`uv run` keeps hook tool versions identical to CI's resolution."""

    for hook_id, hook in local_hooks().items():
        entry = hook["entry"]
        assert entry.startswith("uv run --all-packages --all-extras "), (
            f"{hook_id} must resolve its tool through uv so versions come from "
            f"uv.lock rather than PATH, got {entry!r}"
        )
        assert hook["language"] == "system", f"{hook_id} must use the uv-provided environment"
