"""Non-public IPv6 ranges that ``is_global`` can accept are still refused."""

from __future__ import annotations

from ipaddress import ip_network

import pytest

from harborrag_adapters.connectors.policies.network import (
    BlockedDestinationError,
    DestinationPolicy,
)

pytestmark = pytest.mark.unit

NON_PUBLIC_IPV6 = ["fec0::1", "fec0:1234::5", "3fff::1", "3fff:fff::1", "5f00::1", "5f00:ffff::1"]


@pytest.mark.parametrize("address", NON_PUBLIC_IPV6)
def test_strict_policy_refuses_non_public_ipv6(address: str) -> None:
    with pytest.raises(BlockedDestinationError):
        DestinationPolicy().check("wiki.example.com", address)


@pytest.mark.parametrize("address", ["2606:4700::1111", "2001:4860:4860::8888"])
def test_strict_policy_still_allows_public_ipv6(address: str) -> None:
    DestinationPolicy().check("wiki.example.com", address)


def test_allowed_network_overrides_the_refusal() -> None:
    policy = DestinationPolicy(networks=(ip_network("fec0::/10"),))

    policy.check("wiki.internal", "fec0::1")


def test_allowed_host_overrides_the_refusal() -> None:
    policy = DestinationPolicy(hosts=frozenset({"wiki.internal"}))

    policy.check("wiki.internal", "3fff::1")
