"""Smoke-test a bare wheel in a fresh environment outside the source checkout.

Build the workspace wheels first with ``uv build --all-packages --wheel``.
Only the requested distribution and its declared dependency closure are
installed. Local-wheel constraints prevent resolving first-party packages
from an older published release, without installing unrelated packages.
"""

from __future__ import annotations

import argparse
import email
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

MODULES = {
    "harborrag-core": ["harborrag_core", "harborrag_core.ports", "harborrag_core.indexing"],
    "harborrag-adapters": [
        "harborrag_adapters",
        "harborrag_adapters.connectors.registry",
        "harborrag_adapters.parsers.registry",
        "harborrag_adapters.repositories.state.sql_base",
    ],
    "harborrag-memory": ["harborrag_memory", "harborrag_memory.context"],
    "harborrag-engine": [
        "harborrag_engine",
        "harborrag_engine.config",
        "harborrag_engine.retrieval.pipeline",
    ],
    "harborrag-runtime": [
        "harborrag_runtime",
        "harborrag_runtime.sdk",
        "harborrag_runtime.config.settings",
        "harborrag_runtime.agent.tools",
        "harborrag_runtime.topology.summary_operations",
    ],
    "harborrag-app": [
        "harborrag_app",
        "harborrag_app.cli.main",
        "harborrag_app.workflow_control",
    ],
    "harborrag-mcp-server": ["harborrag_mcp_server", "harborrag_mcp_server.__main__"],
    "harborrag": ["harborrag", "harborrag_app.cli.main"],
}

SMOKE = """
import importlib
import importlib.metadata
import importlib.resources
import json
import pathlib
import sys

package, modules = json.loads(sys.argv[1])
prefix = pathlib.Path(sys.prefix).resolve()
for name in modules:
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(prefix), module.__file__
    if name in {'harborrag', 'harborrag_runtime', 'harborrag_memory'}:
        for attribute in module.__all__:
            getattr(module, attribute)
for distribution in importlib.metadata.distributions():
    direct_url = distribution.read_text('direct_url.json')
    if direct_url:
        assert not json.loads(direct_url).get('dir_info', {}).get('editable'), direct_url
if package == 'harborrag-mcp-server':
    root = importlib.resources.files('harborrag_mcp_server')
    assert root.joinpath('defaults/mcp.yaml').read_text()
    assert root.joinpath('server/static/status.html').read_text()
print(f'{package}: bare installed-wheel imports and resources passed')
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", choices=MODULES)
    parser.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    parser.add_argument("--offline", action="store_true", help="Use only uv's cached dependencies")
    args = parser.parse_args()
    wheels: dict[str, Path] = {}
    for wheel in args.wheel_dir.resolve().glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            metadata = next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            name = email.message_from_bytes(archive.read(metadata))["Name"]
        if name in wheels:
            raise SystemExit(f"More than one wheel for {name}; provide a clean wheel directory")
        wheels[name] = wheel
    missing = set(MODULES) - wheels.keys()
    if missing:
        raise SystemExit(f"Build all workspace wheels first; missing: {', '.join(sorted(missing))}")
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(name, None)
    offline = ["--offline"] if args.offline else []
    with tempfile.TemporaryDirectory(prefix="harbor-installed-wheel-") as temporary:
        work = Path(temporary)
        constraints = work / "first-party-wheels.txt"
        constraints.write_text(
            "".join(f"{name} @ {wheel.as_uri()}\n" for name, wheel in sorted(wheels.items())),
            encoding="utf-8",
        )
        virtualenv = work / "venv"
        subprocess.run(
            ["uv", "venv", *offline, "--python", sys.executable, str(virtualenv)],
            cwd=work,
            env=environment,
            check=True,
        )
        python = virtualenv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                *offline,
                "--python",
                str(python),
                "--constraint",
                str(constraints),
                str(wheels[args.package]),
            ],
            cwd=work,
            env=environment,
            check=True,
        )
        subprocess.run(
            [str(python), "-I", "-c", SMOKE, json.dumps([args.package, MODULES[args.package]])],
            cwd=work,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
