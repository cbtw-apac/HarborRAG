"""Export the OpenAPI schema — the published API contract artifact (ST10).

Usage: python -m harborrag_app.api.export_openapi > openapi.json
CI uploads the result, diffs it against the target branch with oasdiff
(breaking changes block the merge), and generates @harborrag/api-client.
"""

from __future__ import annotations

import json
import sys

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


def export_openapi() -> str:
    """Render the factory app's OpenAPI schema as stable, diff-friendly JSON."""
    # Schema generation never opens a listener. Keep its synthetic application
    # configuration deterministic and compatible with the same fail-closed auth
    # policy enforced by a real development server.
    app = create_fastapi_app(
        ApiSettings(
            env="dev",
            host="127.0.0.1",
            auth_mode="none",
            docs_enabled=False,
        )
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True)


def main() -> int:
    """CLI entrypoint: write the schema to stdout."""
    sys.stdout.write(export_openapi() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
