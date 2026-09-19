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


class TestReachingTheLocalNetwork(unittest.TestCase):

    def _reachable(self, probe, routes=PROC_NET_ROUTE):
        with _routes(routes), patch.object(net, "_tcp_probe", probe):
            return net.lan_reachable()

    def test_a_gateway_that_accepts_is_reachable(self):
        self.assertIs(self._reachable(_Probe()), True)

    def test_a_gateway_that_refuses_is_still_reachable(self):
        # A refusal is proof the host is there and answering: exactly the
        # signal wanted, from a router with nothing listening on the port.
        self.assertIs(self._reachable(_Probe(ConnectionRefusedError())), True)

    def test_a_gateway_that_never_answers_is_not_reachable(self):
        self.assertIs(self._reachable(_Probe(socket.timeout())), False)

    def test_no_route_to_the_gateway_is_not_reachable(self):
        for code in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            with self.subTest(code=errno.errorcode[code]):
                self.assertIs(self._reachable(_Probe(OSError(code, "no"))), False)

    def test_it_probes_the_gateway_and_gives_up_quickly(self):
        probe = _Probe()
        self._reachable(probe)
        (host, port), timeout = probe.asked[0]
        self.assertEqual(host, "192.168.50.1")
        self.assertEqual(port, net.PROBE_PORT)
        self.assertLessEqual(timeout, 3.0)

    def test_without_a_gateway_the_answer_is_unknown_not_false(self):
        # Refusing to guess matters: reporting "your network is down" to
        # someone whose network is fine is worse than the old message.
        probe = _Probe()
        self.assertIsNone(self._reachable(probe, routes=NO_DEFAULT_ROUTE))
        self.assertEqual(probe.asked, [])

    def test_an_unexpected_error_is_unknown_not_false(self):
        self.assertIsNone(self._reachable(_Probe(ValueError("boom"))))


if __name__ == "__main__":
    unittest.main()
