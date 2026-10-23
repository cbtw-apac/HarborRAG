"""Production package sources ship no mock or fake modules.

Test doubles live under each package's ``tests/`` directory, or in an explicit
``testing/`` package such as ``harborrag_core.testing`` when they are shared.
CodeRabbit reviews for this as well, but its findings are advisory; this test
is what blocks a merge.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TEST_DOUBLE_MODULE = re.compile(r"^(?:mocks?|fakes?)(?:_\w+)?\.py$|_(?:mocks?|fakes?)\.py$")
_TEST_DOUBLE_PACKAGES = {"mock", "mocks", "fake", "fakes"}


def _test_double_modules(root: Path) -> list[str]:
    found = []
    for path in sorted(root.glob("packages/*/src/**/*.py")):
        relative = path.relative_to(root)
        if "testing" in relative.parts:
            continue
        if _TEST_DOUBLE_MODULE.search(path.name) or _TEST_DOUBLE_PACKAGES & set(relative.parts):
            found.append(relative.as_posix())
    return found


def test_production_sources_ship_no_test_doubles() -> None:
    assert _test_double_modules(ROOT) == []


def test_test_doubles_are_detected_outside_testing_packages(tmp_path: Path) -> None:
    source = tmp_path / "packages" / "harborrag-demo" / "src" / "harborrag_demo"
    for relative in (
        "connectors/mock.py",
        "repositories/vector/fake_qdrant.py",
        "models/client_mock.py",
        "fakes/store.py",
        "repositories/vector/mock/__init__.py",
        "repositories/cache/fake/backend.py",
        "testing/fakes.py",
        "connectors/mockingbird_parser.py",
    ):
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text("", encoding="utf-8")

    assert _test_double_modules(tmp_path) == [
        "packages/harborrag-demo/src/harborrag_demo/connectors/mock.py",
        "packages/harborrag-demo/src/harborrag_demo/fakes/store.py",
        "packages/harborrag-demo/src/harborrag_demo/models/client_mock.py",
        "packages/harborrag-demo/src/harborrag_demo/repositories/cache/fake/backend.py",
        "packages/harborrag-demo/src/harborrag_demo/repositories/vector/fake_qdrant.py",
        "packages/harborrag-demo/src/harborrag_demo/repositories/vector/mock/__init__.py",
    ]
