"""Run the API on its public port and its ops port in one process.

Usage: ``python -m harborrag_app.api.serve``. Both sockets serve one application and one
lifespan; ``OpsPortMiddleware`` keeps ``/metrics`` on the ops socket and the API off it.
"""

from __future__ import annotations

import socket

import uvicorn

from .settings import ApiSettings


def _listen(host: str, port: int) -> socket.socket:
    """Open a listening TCP socket on ``host:port``."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family)


def bind_sockets(settings: ApiSettings) -> list[socket.socket]:
    """Open the public socket, and the ops socket when an ops port is set.

    Args:
        settings: API settings naming the host and both ports.

    Returns:
        The listening sockets, public first.
    """
    sockets = [_listen(settings.host, settings.port)]
    if settings.ops_port is not None:
        try:
            sockets.append(_listen(settings.host, settings.ops_port))
        except OSError:
            sockets[0].close()
            raise
    return sockets


def main() -> None:
    """Serve the API factory on every configured socket until stopped."""
    settings = ApiSettings()
    config = uvicorn.Config(
        "harborrag_app.api.app:create_fastapi_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )
    uvicorn.Server(config).run(sockets=bind_sockets(settings))


if __name__ == "__main__":
    main()
