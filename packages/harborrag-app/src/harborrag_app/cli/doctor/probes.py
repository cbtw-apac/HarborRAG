"""Network probes for local services, standard library only.

Each probe returns ``None`` on success or a short error string, so the suite can turn it
into a check without importing a provider client that may not be installed.
"""

from __future__ import annotations

import importlib.util
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 3.0
_HTTP_OK_UPPER_BOUND = 400
_S3_CODE = re.compile(r"[A-Za-z0-9]{1,64}")


def tcp_reachable(host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return f"{host}:{port} unreachable ({exc.strerror or exc})"


def safe_url(url: str) -> str:
    """Scheme, host, port and path only.

    A configured endpoint may embed userinfo or a token query parameter, and every error
    string below reaches ``Check.detail`` and the JSON doctor output.
    """

    try:
        parsed = urlparse(url)
    except ValueError:
        return "the configured URL"
    if not parsed.scheme or not parsed.hostname:
        return "the configured URL"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}"


def http_ok(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - health URL from settings
    shown = safe_url(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return f"{shown} returned HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{shown} unreachable ({getattr(exc, 'reason', exc)})"
    if status < _HTTP_OK_UPPER_BOUND:
        return None
    return f"{shown} returned HTTP {status}"


def redis_ping(host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """RESP ``PING`` without a Redis client; FalkorDB speaks the same protocol."""

    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            reply = connection.recv(64)
    except OSError as exc:
        return f"{host}:{port} unreachable ({exc.strerror or exc})"
    if reply.startswith(b"+PONG"):
        return None
    return f"{host}:{port} answered {reply[:32]!r} instead of PONG"


def s3_credentials(
    endpoint: str,
    region: str | None,
    key: str | None,
    secret: str | None,
    token: str | None,
) -> str | None:
    """Sign a real ``ListBuckets`` call; ``None`` is OK, else only the S3 error code.

    The code is the whole answer: botocore's message can echo the access key id, and the
    caller must never see the credentials or the endpoint's userinfo.
    """

    if importlib.util.find_spec("botocore") is None:
        return None
    import botocore.session
    from botocore.config import Config
    from botocore.exceptions import (
        ClientError,
        NoCredentialsError,
    )

    try:
        client = botocore.session.get_session().create_client(
            "s3",
            endpoint_url=endpoint,
            region_name=region or "us-east-1",
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            aws_session_token=token,
            config=Config(
                connect_timeout=DEFAULT_TIMEOUT,
                read_timeout=DEFAULT_TIMEOUT,
                retries={"max_attempts": 1},
            ),
        )
        client.list_buckets()
    except NoCredentialsError:
        return "NoCredentials"
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return code if _S3_CODE.fullmatch(code) else "S3Error"
    except Exception:  # noqa: BLE001 - a probe reports, it never raises
        return "unreachable"
    return None


def host_port(url: str, *, default_port: int) -> tuple[str, int]:
    parsed = urlparse(url)
    return parsed.hostname or "localhost", parsed.port or default_port


__all__ = [
    "DEFAULT_TIMEOUT",
    "host_port",
    "http_ok",
    "redis_ping",
    "s3_credentials",
    "safe_url",
    "tcp_reachable",
]
