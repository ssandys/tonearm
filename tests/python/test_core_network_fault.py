"""Telling "the Core is off" apart from "this machine has no network".

Both arrive as a connect that never answers, and until now both were
reported as "Roon Core unreachable" -- which points whoever is reading the
bar at the Core when the Core may be perfectly healthy. Measured on
2026-09-07: a VPN exit node swallowed the local subnet for five hours while
the Core sat 1.9ms away, and nothing in the daemon said so.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import core   # noqa: E402


class FakeSocket:
    def __init__(self, connected=True):
        self.connected = connected


class FakeApi:
    def __init__(self, connected=True):
        self._roonsocket = FakeSocket(connected)
        self.zones = {}


def session(status="ok"):
    published = []
    s = core.RoonSession(published.append)
    s._api = FakeApi()
    s._status = status
    s._cfg = {"host": "192.168.50.118", "tcp_port": 9100,
              "http_port": 9330, "name": "yavin"}
    return s, published


def lan(verdict):
    """Pin what the local-network check concludes.

    `verdict` keeps the meaning it has always had here -- True is a healthy
    LAN -- while the mechanism under it changed in #11. It used to be "did the
    default gateway answer TCP", which plenty of healthy gateways do not; it
    is now "is Core traffic leaving by an interface that cannot reach the
    Core's own subnet". Note the sense inverts: a healthy LAN is now `False`
    from routed_off_lan, because that function reports a FAULT.
    """
    fault = {True: False, False: True, None: None}[verdict]
    return patch.object(core.net, "routed_off_lan",
                        lambda *_args, **_kw: fault)


def drop(s):
    """Take the socket down for as many polls as it takes to be believed."""
    s._api._roonsocket.connected = False
    for _ in range(core.DOWN_SAMPLES):
        s._check_connection()


class TestTheWatcherNamesTheRightFault(unittest.TestCase):

    def test_a_reachable_gateway_still_blames_the_core(self):
        s, _ = session()
        with lan(True):
            drop(s)
        self.assertEqual(s.status, "unreachable")

    def test_an_unreachable_gateway_blames_the_network(self):
        s, _ = session()
        with lan(False):
            drop(s)
        self.assertEqual(s.status, "no_network")

    def test_an_inconclusive_probe_falls_back_to_the_old_wording(self):
        # Guessing "your network is down" at someone whose network is fine
        # is worse than saying nothing new.
        s, _ = session()
        with lan(None):
            drop(s)
        self.assertEqual(s.status, "unreachable")

    def test_the_new_status_reaches_the_bar(self):
        s, published = session()
        with lan(False):
            drop(s)
        self.assertEqual(published[-1]["status"], "no_network")

    def test_the_probe_runs_only_on_the_transition(self):
        # It is a blocking connect inside a 2s poll loop; running it every
        # poll would put a network round trip in the daemon's hot path.
        calls = []

        def probe(*_args, **_kw):
            calls.append(1)
            return True          # a fault, i.e. the old probe's False

        s, _ = session()
        with patch.object(core.net, "routed_off_lan", probe):
            drop(s)
            for _ in range(5):
                s._check_connection()
        self.assertEqual(len(calls), 1)


class TestTheFaultIsReassessed(unittest.TestCase):
    """A fault that changes character while the socket stays down."""

    def setUp(self):
        # These tests poll past RELOCATE_SAMPLES, which is where the watcher
        # looks for a moved Core. Without this stub that is real multicast on
        # the real network, in a unit test: slow, and answered by whatever
        # happens to be listening on the machine running the suite.
        patcher = patch.object(core.sood, "discover", lambda **_: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _down_for(self, s, polls):
        for _ in range(polls):
            s._check_connection()

    def test_a_network_that_returns_stops_blaming_the_network(self):
        s, published = session()
        with lan(False):
            drop(s)
        self.assertEqual(s.status, "no_network")

        # The network comes back; the Core is still off.
        with lan(True):
            self._down_for(s, core.RELOCATE_SAMPLES)
        self.assertEqual(s.status, "unreachable")
        self.assertEqual(published[-1]["status"], "unreachable")

    def test_a_network_that_fails_later_starts_blaming_the_network(self):
        s, _ = session()
        with lan(True):
            drop(s)
        self.assertEqual(s.status, "unreachable")

        with lan(False):
            self._down_for(s, core.RELOCATE_SAMPLES)
        self.assertEqual(s.status, "no_network")

    def test_an_unchanged_fault_is_not_republished(self):
        s, published = session()
        with lan(False):
            drop(s)
        before = len(published)
        with lan(False):
            self._down_for(s, core.RELOCATE_SAMPLES)
        self.assertEqual(len(published), before)


class TestRecoveryStillWorks(unittest.TestCase):

    def test_a_network_fault_recovers_when_the_socket_returns(self):
        # The watcher ignores any status outside the set it owns, so adding
        # a status without adding it there would freeze the daemon in the
        # fault forever -- it would never report "ok" again.
        s, _ = session()
        with lan(False):
            drop(s)
        self.assertEqual(s.status, "no_network")

        s._api._roonsocket.connected = True
        s._check_connection()
        self.assertEqual(s.status, "ok")


class TestStartNamesTheRightFault(unittest.TestCase):

    def _start_failing(self, verdict):
        """start() with every connect refused and no relocated Core."""
        published = []
        s = core.RoonSession(published.append)
        s._cfg = {"host": "192.168.50.118", "tcp_port": 9100,
                  "http_port": 9330, "name": "yavin"}
        with patch.object(core.RoonSession, "_connect", lambda _self, _t: None), \
             patch.object(core.RoonSession, "_find_relocated", lambda _self: None), \
             patch.object(core.config, "load_token", lambda: "tok"), \
             lan(verdict):
            with self.assertRaises(SystemExit) as caught:
                s.start()
        return s, published, caught.exception.code

    def test_a_reachable_gateway_still_blames_the_core(self):
        s, published, code = self._start_failing(True)
        self.assertEqual(s.status, "unreachable")
        self.assertEqual(published[-1]["status"], "unreachable")
        self.assertEqual(code, 1)

    def test_an_unreachable_gateway_blames_the_network(self):
        s, published, code = self._start_failing(False)
        self.assertEqual(s.status, "no_network")
        self.assertEqual(published[-1]["status"], "no_network")
        # Still a failure exit: systemd restarts and retries either way.
        self.assertEqual(code, 1)

    def test_an_inconclusive_probe_falls_back_to_the_old_wording(self):
        s, _, _ = self._start_failing(None)
        self.assertEqual(s.status, "unreachable")


if __name__ == "__main__":
    unittest.main()


class TestTheFaultIsOnlyNamedWhenItIsKnown(unittest.TestCase):
    """#11: the old discriminator blamed the network on healthy networks.

    It asked whether the default gateway answered TCP, and plenty of healthy
    gateways do not. CI is the proof: after the `no_network` status landed,
    eleven pre-existing tests failed on a GitHub runner with

        AssertionError: 'no_network' != 'unreachable'

    because that runner's gateway ignores tcp/80. Its network was perfect.

    These drive the real decision rather than pinning its verdict, patching
    only the two facts it reads.
    """

    def _status(self, host, source, nets):
        with patch.object(core.net, "source_address_for", lambda _h: source), \
             patch.object(core.net, "local_networks", lambda: nets):
            return core._unreachable_status(host)

    def test_a_core_on_no_local_subnet_is_not_a_network_fault(self):
        # The CI shape: a runner on 10.1.0.0/16 asked about a Core that is not
        # on any subnet it holds. Nothing can be concluded, so nothing is
        # claimed -- which is the whole of this fix.
        self.assertEqual(
            self._status("192.168.50.118", "10.1.0.4", ["10.1.0.0/16"]),
            "unreachable")

    def test_a_tunnel_swallowing_the_cores_subnet_is_a_network_fault(self):
        # Measured 2026-09-07: an exit node captured 192.168.50.0/24 for five
        # hours while the Core sat 1.9ms away, and the daemon blamed the Core
        # the entire time. This is the condition that should be named.
        self.assertEqual(
            self._status("192.168.50.118", "100.94.206.126",
                         ["192.168.50.0/24", "100.64.0.0/10"]),
            "no_network")

    def test_a_healthy_lan_blames_the_core(self):
        self.assertEqual(
            self._status("192.168.50.118", "192.168.50.23", ["192.168.50.0/24"]),
            "unreachable")

    def test_no_configured_host_cannot_accuse_the_network(self):
        # start() reaches here with no host at all when discovery found
        # nothing. There is no address to reason about.
        self.assertEqual(self._status(None, None, ["192.168.50.0/24"]),
                         "unreachable")
