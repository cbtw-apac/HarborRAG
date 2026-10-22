"""Sign in to SharePoint for ``auth_mode: device_code`` connections.

Usage::

    python -m harborrag_adapters.connectors.sharepoint.login \
        --tenant-id <tenant-guid> --client-id <app-guid>

Tenant and client IDs default to ``MICROSOFT_TENANT_ID`` and
``MICROSOFT_CLIENT_ID``. ``--logout`` deletes the stored sign-in.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from harborrag_adapters.connectors.exceptions import AuthenticationError

from .delegated import DelegatedTokenCache, DeviceCodeLogin, default_token_cache_path


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant-id", default=os.getenv("MICROSOFT_TENANT_ID"))
    parser.add_argument("--client-id", default=os.getenv("MICROSOFT_CLIENT_ID"))
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
    args = _arguments(argv)
    if not args.tenant_id or not args.client_id:
        print(
            "--tenant-id and --client-id (or MICROSOFT_TENANT_ID/MICROSOFT_CLIENT_ID) are required"
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
