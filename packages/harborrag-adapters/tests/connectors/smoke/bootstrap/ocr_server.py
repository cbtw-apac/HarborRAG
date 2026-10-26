"""Retarget the configured PDF OCR server at a URL this machine can reach.

`config/parsers.yaml` is written for the containerized ingestion worker: the
enabled `pdf-liteparse` definition points `ocr_server_url` at the
`ppocr-server` Docker DNS alias, which only resolves inside
`harborrag-data-network`. These smoke scripts run on the host, where that name
does not resolve and LiteParse fails every scanned page with
`OCR failed: ... error sending request`.

So the configured hostname is swapped for the loopback interface (keeping the
scheme, port, and path) whenever this machine cannot resolve it, which leaves a
run inside the container untouched. Set `HARBOR_SMOKE_OCR_SERVER_URL` to point
at a different OCR server instead, or `HARBORRAG_OCR_SERVER_URL` to move the
server for every process that loads the catalog.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .environment import env

if TYPE_CHECKING:
    from harborrag_adapters.parsers import HarborParserRegistry

OCR_SERVER_URL_VARIABLE = "HARBOR_SMOKE_OCR_SERVER_URL"
LOOPBACK_HOST = "localhost"


def _resolvable(hostname: str) -> bool:
    """Report whether this machine can resolve `hostname` to an address."""
    try:
        socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    return True


def _loopback_netloc(parsed: SplitResult) -> str:
    """Rebuild one URL's authority against the loopback host.

    Userinfo and the port are preserved: only the hostname is container-local,
    and an OCR server published on the host keeps the same port number.
    """
    credentials = ""
    if parsed.username:
        credentials = parsed.username
        if parsed.password:
            credentials += f":{parsed.password}"
        credentials += "@"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{credentials}{LOOPBACK_HOST}{port}"


def host_ocr_server_url(configured: str | None) -> str | None:
    """Return the OCR server URL to use from the machine running the smoke check.

    An explicit `HARBOR_SMOKE_OCR_SERVER_URL` always wins. Otherwise the
    configured URL is returned unchanged unless its hostname is unresolvable
    here, in which case only the host is replaced with the loopback interface.
    """
    override = env(OCR_SERVER_URL_VARIABLE)
    if override:
        return override
    if not configured:
        return configured

    parsed = urlsplit(configured)
    hostname = parsed.hostname
    if not hostname or hostname == LOOPBACK_HOST or _resolvable(hostname):
        return configured
    return urlunsplit(parsed._replace(netloc=_loopback_netloc(parsed)))


def apply_host_ocr_server_url(harbor_parser: HarborParserRegistry) -> None:
    """Rewrite every configured engine's OCR server URL for this host.

    Only engines that expose an `ocr_server_url` are touched, and a rewrite is
    announced so a smoke run never silently parses against a different OCR
    service than `config/parsers.yaml` names. The setting lives on an
    `options` object for PDF backends but directly on the engine for the
    image family, so both shapes are checked.
    """
    for family in harbor_parser.families():
        for engine in getattr(family, "engines", ()):
            options = getattr(engine, "options", None)
            holder = engine if getattr(engine, "ocr_server_url", None) else options
            configured = getattr(holder, "ocr_server_url", None)
            if not configured:
                continue
            resolved = host_ocr_server_url(configured)
            if resolved == configured:
                continue
            holder.ocr_server_url = resolved
            print(
                f"[parsers] {engine.name} ocr_server_url={configured!r} is not reachable "
                f"from this host; using {resolved!r}"
            )
