"""Connector traffic stays off private networks unless the operator allows a host."""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator

import pytest
import requests

from harborrag_adapters.connectors.policies.network import (
    ALLOWED_HOSTS_VARIABLE,
    USE_PROXY_VARIABLE,
    BlockedDestinationError,
    DestinationPolicy,
    guarded_session,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.18.0.1",
        "192.168.1.5",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "::ffff:10.0.0.1",
        "100.64.0.1",
        "0.0.0.0",
    ],
)
def test_private_loopback_link_local_and_reserved_addresses_are_blocked(address: str) -> None:
    with pytest.raises(BlockedDestinationError, match=ALLOWED_HOSTS_VARIABLE):
        DestinationPolicy().check("jira.acme.internal", address)


def test_public_addresses_pass_and_allowed_hosts_or_networks_open_private_ones() -> None:
    policy = DestinationPolicy.from_environ(
        {ALLOWED_HOSTS_VARIABLE: " Jira.Acme.Internal , 10.20.0.0/16 ,"}
    )

    DestinationPolicy().check("api.github.com", "140.82.112.6")
    policy.check("jira.acme.internal.", "10.9.9.9")
    policy.check("confluence.acme.internal", "10.20.3.4")
    with pytest.raises(BlockedDestinationError):
        policy.check("confluence.acme.internal", "10.21.3.4")
    assert DestinationPolicy.from_environ({}) == DestinationPolicy()


@pytest.fixture
def local_server() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://localhost:{server.server_port}/"
    server.shutdown()


def test_the_session_checks_the_address_it_actually_connected_to(local_server: str) -> None:
    with pytest.raises(BlockedDestinationError, match="private address 127.0.0.1"):
        guarded_session(DestinationPolicy()).get(local_server, timeout=5)

    allowed = guarded_session(DestinationPolicy(hosts=frozenset({"localhost"})))
    assert allowed.get(local_server, timeout=5).status_code == 200


def test_a_blocked_destination_is_not_retried() -> None:
    assert BlockedDestinationError("x").retryable is False


def test_a_closed_private_port_reads_the_same_as_an_open_one() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(BlockedDestinationError, match="private address 127.0.0.1"):
        guarded_session(DestinationPolicy()).get(f"https://localhost:{port}/", timeout=5)


@pytest.fixture
def dead_proxy(monkeypatch: pytest.MonkeyPatch) -> str:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    proxy = f"http://127.0.0.1:{probe.getsockname()[1]}"
    probe.close()
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    return proxy


def test_a_proxy_variable_does_not_switch_the_guard_off(local_server: str, dead_proxy: str) -> None:
    with pytest.raises(BlockedDestinationError, match="private address 127.0.0.1"):
        guarded_session(DestinationPolicy()).get(local_server, timeout=5)


def test_an_opted_in_proxy_still_refuses_a_private_target(
    local_server: str, dead_proxy: str
) -> None:
    proxied = guarded_session(DestinationPolicy(use_proxy=True))

    with pytest.raises(BlockedDestinationError, match="private address 127.0.0.1"):
        proxied.get(local_server, timeout=5)
    with pytest.raises(requests.exceptions.ProxyError):
        proxied.get("http://93.184.216.34/", timeout=5)


def test_proxies_are_used_only_when_the_operator_opts_in() -> None:
    assert DestinationPolicy.from_environ({USE_PROXY_VARIABLE: " True "}).use_proxy is True
    assert DestinationPolicy.from_environ({USE_PROXY_VARIABLE: "no"}).use_proxy is False
    assert DestinationPolicy.from_environ({}).use_proxy is False
