"""Sign in to SharePoint for ``auth_mode: device_code`` connections.

Usage::

    python -m harborrag_adapters.connectors.sharepoint.login \
        --tenant-id <tenant-guid> --client-id <app-guid>

Tenant and client IDs default to ``MICROSOFT_TENANT_ID`` and
``MICROSOFT_CLIENT_ID``, from the environment or else from the connector env file
(``env/.env.connector``, or ``$CONNECTOR_ENV_FILE``) the CLI reads. ``--logout``
deletes the stored sign-in.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from harborrag_adapters.connectors.exceptions import AuthenticationError

from .delegated import DelegatedTokenCache, DeviceCodeLogin, default_token_cache_path

_TENANT_ID = "MICROSOFT_TENANT_ID"
_CLIENT_ID = "MICROSOFT_CLIENT_ID"
_CONNECTOR_ENV_FILE = "env/.env.connector"


def _connector_env_file() -> Path:
    """The connector env file the CLI and dev.sh use, honouring ``CONNECTOR_ENV_FILE``."""
    candidate = Path(os.environ.get("CONNECTOR_ENV_FILE") or _CONNECTOR_ENV_FILE)
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def _file_ids(path: Path) -> dict[str, str]:
    """Only the tenant and client IDs from ``path``; nothing is exported."""
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - dotenv ships with the CLI
        return {}
    values = dotenv_values(path)
    return {name: value for name in (_TENANT_ID, _CLIENT_ID) if (value := values.get(name))}


def _default(name: str, file_ids: dict[str, str]) -> str | None:
    # An exported variable outranks the file, as it does for the CLI.
    return os.getenv(name) or file_ids.get(name)


def _arguments(argv: list[str] | None, file_ids: dict[str, str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant-id", default=_default(_TENANT_ID, file_ids))
    parser.add_argument("--client-id", default=_default(_CLIENT_ID, file_ids))
    parser.add_argument(
        "--token-cache-path",
        type=Path,
        default=None,
        help="Refresh token file; must match the connection's token_cache_path.",
    )
    parser.add_argument("--logout", action="store_true", help="Delete the stored sign-in.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the device code sign-in, or delete the stored sign-in."""
    env_file = _connector_env_file()
    args = _arguments(argv, _file_ids(env_file))
    if not args.tenant_id or not args.client_id:
        print(
            "--tenant-id and --client-id (or MICROSOFT_TENANT_ID/MICROSOFT_CLIENT_ID, exported "
            f"or in {env_file}) are required"
        )
        return 2
    cache = DelegatedTokenCache(
        path=(
            args.token_cache_path or default_token_cache_path(args.tenant_id, args.client_id)
        ).expanduser(),
        tenant_id=args.tenant_id,
        client_id=args.client_id,
    )
    if args.logout:
        cache.clear()
        print(f"Removed SharePoint sign-in at {cache.path}")
        return 0
    try:
        account = DeviceCodeLogin(cache).run(prompt=lambda message: print(message, flush=True))
    except AuthenticationError as exc:
        print(str(exc))
        return 1
    print(f"Signed in as {account or 'unknown account'}; token stored at {cache.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
