"""Direct-mode commands must not require the Temporal client to be installed."""

from __future__ import annotations

import subprocess
import sys

_BLOCK_TEMPORALIO = "import sys; sys.modules['temporalio'] = None; "


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_TEMPORALIO + code],
        capture_output=True,
        text=True,
        check=False,
    )


def test_app_service_module_imports_without_temporalio() -> None:
    result = _run(
        "import harborrag_app.workflow_control.composition.service as s; "
        "from harborrag_app.cli import main; print('imported')"
    )
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout


def test_connecting_the_temporal_client_without_temporalio_explains_the_extra() -> None:
    result = _run(
        "import asyncio\n"
        "from harborrag_app.workflow_control.composition.factories import connect_temporal_client\n"
        "from harborrag_app.workflow_control.errors import MissingOptionalDependencyError, public_error_message\n"
        "try:\n"
        "    asyncio.run(connect_temporal_client(None))\n"
        "except MissingOptionalDependencyError as exc:\n"
        "    print('friendly:', public_error_message(exc))\n"
    )
    assert result.returncode == 0, result.stderr
    assert 'pip install "harborrag[temporal]"' in result.stdout
