"""Best-effort auto-load of a repo-root ``.env`` for the live smoke suite.

Optional: if ``python-dotenv`` isn't installed, export the variables in
``.env.example`` yourself (e.g. ``set -a && source .env && set +a``) before
running pytest. ``load_dotenv`` never overrides variables already present in
the environment, so explicit exports always win over the file.
"""
from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    _repo_root = Path(__file__).resolve().parents[4]
    load_dotenv(_repo_root / ".env")
