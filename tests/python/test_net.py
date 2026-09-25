"""The local-network probe that tells a dead Core apart from a dead route.

Both look identical from the daemon: a connect that never answers. The
difference matters to whoever is reading the bar, because one of them is
fixed by turning the Core on and the other is not.
"""

import errno
import socket
import unittest
from unittest.mock import patch

from scripts.tonearm_lib import net


# Captured verbatim from a running machine on 2026-09-07, the day this was
# written -- including the trailing padding the kernel emits.
PROC_NET_ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "wlp192s0\t00000000\t0132A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
    "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    "wlp192s0\t0032A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
)

NO_DEFAULT_ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)

# A default row whose RTF_GATEWAY bit is clear: a point-to-point link with no
# next hop. There is no address to probe, so it must not be read as one.
DEFAULT_WITHOUT_GATEWAY = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "tun0\t00000000\t00000000\t0001\t0\t0\t50\t00000000\t0\t0\t0\n"
)


def _routes(text):
    """Patch the file the gateway is read out of."""
    return patch.object(net, "_read_proc_route", lambda: text)


class TestFindingTheGateway(unittest.TestCase):

    def test_it_decodes_the_little_endian_gateway_address(self):
        with _routes(PROC_NET_ROUTE):
            self.assertEqual(net.default_gateway(), "192.168.50.1")

    def test_a_machine_with_no_default_route_has_no_gateway(self):
        with _routes(NO_DEFAULT_ROUTE):
            self.assertIsNone(net.default_gateway())

    def test_a_default_route_with_no_next_hop_is_not_a_gateway(self):
        with _routes(DEFAULT_WITHOUT_GATEWAY):
            self.assertIsNone(net.default_gateway())

    def test_an_unreadable_proc_is_not_an_exception(self):
        # /proc/net/route is readable under the unit's sandbox (verified
        # 2026-09-07), but this runs on the failure path and must never be
        # the reason the daemon dies.
        with patch.object(net, "_read_proc_route", side_effect=OSError):
            self.assertIsNone(net.default_gateway())


class _Probe:
    """Stands in for the outbound connect, recording what it was asked."""

    def __init__(self, raise_with=None):
        self.raise_with = raise_with
        self.asked = []

    def __call__(self, address, timeout):
        self.asked.append((address, timeout))
        if self.raise_with is not None:
            raise self.raise_with


# TestReachingTheLocalNetwork and its `lan_reachable` probe were removed in
# #11. The probe inferred a network fault from a default gateway that did not
# answer TCP, and plenty of healthy gateways do not -- CI proved it, failing
# eleven tests on a GitHub runner whose network was perfect. The tests were
# correct about the probe; the probe was wrong about the network. What
# replaced it is below: a routing fact rather than a third party's manners.
#
# `_Probe` above is kept for `default_gateway`'s own tests.


class TestWhichNetworksAreOnLink(unittest.TestCase):
    """The non-default rows of the main table are this machine's own subnets.

    Read from /proc/net/route rather than enumerated from interfaces, because
    that file is already read here and is already verified readable inside the
    unit's sandbox -- and because it carries the real prefix rather than an
    assumed /24.
    """

    def test_it_returns_the_on_link_subnets_with_their_real_prefixes(self):
        with _routes(PROC_NET_ROUTE):
            nets = [str(n) for n in net.local_networks()]
        self.assertIn("192.168.50.0/24", nets)
        self.assertIn("172.17.0.0/16", nets)

    def test_the_default_route_is_not_a_subnet(self):
        # 0.0.0.0/0 contains every address, so letting it through would make
        # every Core look on-link and the whole check vacuous.
        with _routes(PROC_NET_ROUTE):
            self.assertNotIn("0.0.0.0/0", [str(n) for n in net.local_networks()])

    def test_host_routes_are_not_subnets(self):
        # Seen on a live table: a /32 for the default gateway sits alongside
        # the /24. A host route says how to reach ONE address, not that this
        # machine is on a subnet with it -- and treating it as a subnet is
        # actively harmful, because a source address can never be inside a
        # /32 that names something else, so any Core reachable only by a host
        # route would be reported as a routing fault.
        table = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
            "wlp192s0\t0132A8C0\t00000000\t0005\t0\t0\t600\tFFFFFFFF\t0\t0\t0\n"
        )
        with _routes(table):
            self.assertEqual(net.local_networks(), [])

    def test_an_unreadable_route_table_is_not_an_exception(self):
        def boom():
            raise OSError("nope")
        with patch.object(net, "_read_proc_route", boom):
            self.assertEqual(net.local_networks(), [])


class TestTellingARoutingFaultFromAnAbsentCore(unittest.TestCase):
    """#11: only claim `no_network` when it can be positively established.

    The gateway probe this replaces inferred a network fault from a gateway
    that did not answer TCP -- and plenty of healthy gateways do not. CI
    proved it: eleven tests failed on a GitHub runner whose network was
    perfect and whose gateway ignores tcp/80.

    The replacement asks the kernel which source address it would use to
    reach the Core. That is a routing fact, not a third party's manners. But
    the obvious test built on it -- "is the source in the Core's /24?" -- has
    its own false positive, so the rule is narrower: a fault is only claimed
    when the Core is on a subnet this machine HAS, and the kernel is leaving
    by some other one.
    """

    HOME = ["192.168.50.0/24", "172.17.0.0/16"]

    def test_a_source_on_the_cores_own_subnet_is_healthy(self):
        self.assertIs(
            net.routed_off_lan("192.168.50.118", "192.168.50.23", self.HOME),
            False)

    def test_leaving_by_a_tunnel_while_the_core_is_on_a_local_subnet_is_a_fault(self):
        # Measured 2026-09-07: a Tailscale exit node captured 192.168.50.0/24
        # and every connect timed out for five hours while the Core sat 1.9ms
        # away. This is that condition, observed directly.
        self.assertIs(
            net.routed_off_lan("192.168.50.118", "100.94.206.126", self.HOME),
            True)

    def test_a_core_on_no_local_subnet_is_not_called_a_network_fault(self):
        # The false positive that sank the obvious version: a Core reached
        # through a router -- another VLAN, a wired segment -- legitimately
        # has a source address outside its own /24. Measured here: asking for
        # 10.99.99.1 yields the default route's source, and calling that
        # `no_network` would be the same wrong message inverted.
        self.assertIsNone(
            net.routed_off_lan("10.99.99.1", "192.168.50.23", self.HOME))

    def test_no_source_address_is_unknown_rather_than_a_fault(self):
        self.assertIsNone(
            net.routed_off_lan("192.168.50.118", None, self.HOME))

    def test_a_hostname_cannot_be_reasoned_about(self):
        # config accepts a DNS name for the Core; this check needs an address.
        self.assertIsNone(
            net.routed_off_lan("core.local", "192.168.50.23", self.HOME))

    def test_no_known_subnets_is_unknown(self):
        self.assertIsNone(net.routed_off_lan("192.168.50.118", "192.168.50.23", []))
