"""The HarborRAG CLI must import and run without the [api extras]"""

import builtins
import importlib
import subprocess
import sys

import pytest


@pytest.mark.smoke
def test_cli_imports_without_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    api_only_modules = (
        "fastapi",
        "starlette",
        "uvicorn",
        "sse_starlette",
        "websockets",
    )
    """Importing the CLI entrypoint succeeds when API-only dependencies are absent."""
    for name in list(sys.modules):
        if name.split(".")[0] in api_only_modules or name.startswith("harborrag_app"):
            monkeypatch.delitem(sys.modules, name)

    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] in api_only_modules:
            raise ModuleNotFoundError(f"No module named {name!r} (blocked by test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    module = importlib.import_module("harborrag_app.cli.main")
    assert module is not None


@pytest.mark.smoke
def test_ingestion_cli_service_does_not_import_model_provider_runtime() -> None:
    """Temporal submission must not initialize LiteLLM or perform model-network I/O."""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import harborrag_app.workflow_control.client; "
                "assert 'litellm' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
