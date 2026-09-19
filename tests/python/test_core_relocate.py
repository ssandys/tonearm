"""Recovering when the Roon Core changes IP address.

Measured on 2026-09-06: the Core (`yavin`) took a new DHCP lease overnight and
moved from 192.168.50.118 to .119. `~/.config/tonearm/config.json` still
pinned the old address, so the vendored roonapi retried it every ~21s for
three hours while the bar read "Roon Core unreachable". Nothing could recover
it: `start()` ran discovery only when `host` was ABSENT (core.py), so an
address once stored was retried forever, and `_watch_connection` deliberately
never exits because roonapi's own reconnect usually works.

Both of those properties are worth keeping -- this adds one narrow escape.
When the Core is unreachable, ask the network where it is; adopt the answer
ONLY when it is identifiably the same Core (`unique_id`, else name) at a
different address. A stranger's Core on the same LAN must never be able to
capture this daemon, and a Core that has merely rebooted must not cost a
process restart.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import unittest.mock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import config   # noqa: E402
from tonearm_lib import core     # noqa: E402


def a_core(host="192.168.50.119", name="yavin", unique_id="uid-yavin"):
    """One entry shaped exactly as `sood.discover()` returns it."""
    return {"host": host, "name": name, "unique_id": unique_id,
            "tcp_port": 9150, "http_port": 9330,
            "display_version": "2.71 (build 1683) production",
            "via": "multicast"}


def a_config(host="192.168.50.118", **kw):
    cfg = {"host": host, "tcp_port": 9150, "http_port": 9330,
           "name": "yavin", "pinned_zone_id": None, "unique_id": None}
    cfg.update(kw)
    return cfg


class TestChoosingTheCoreToAdopt(unittest.TestCase):
    """`_relocated_core` is the whole decision, and it is pure.

    Factored out of the two callers (`start()` and the connection watcher)
    so the "is this really our Core?" rule has its own regression test
    instead of only being reachable with a live LAN.
    """

    def test_a_stored_unique_id_is_matched_exactly(self):
        cfg = a_config(unique_id="uid-yavin")
        found = core._relocated_core(cfg, [a_core()])
        self.assertEqual(found["host"], "192.168.50.119")

    def test_a_different_unique_id_is_refused_even_when_the_name_matches(self):
        # Two Cores can share a display name; the id is the identity. A name
        # collision must never be enough to move a pairing token.
        cfg = a_config(unique_id="uid-yavin")
        self.assertIsNone(
            core._relocated_core(cfg, [a_core(unique_id="uid-someone-else")]))

    def test_the_name_identifies_the_core_when_no_unique_id_is_stored(self):
        # Every config written before this change is in this state.
        self.assertIsNone(a_config()["unique_id"])
        found = core._relocated_core(a_config(), [a_core()])
        self.assertEqual(found["host"], "192.168.50.119")

    def test_a_stranger_core_on_the_same_lan_is_refused(self):
        self.assertIsNone(
            core._relocated_core(a_config(), [a_core(name="neighbour",
                                                     unique_id="uid-theirs")]))

    def test_the_same_address_is_not_a_relocation(self):
        # A Core that is merely rebooting answers discovery at the address we
        # already have. Adopting that would restart the daemon for nothing.
        cfg = a_config(host="192.168.50.119")
        self.assertIsNone(core._relocated_core(cfg, [a_core()]))

    def test_a_changed_port_at_the_same_address_is_a_relocation(self):
        cfg = a_config(host="192.168.50.119", http_port=9330)
        found = core._relocated_core(cfg, [a_core(host="192.168.50.119")
                                           | {"http_port": 9331}])
        self.assertEqual(found["http_port"], 9331)

    def test_a_config_with_no_identity_adopts_a_lone_core(self):
        # A hand-written config: an address and nothing else. One Core on the
        # LAN is unambiguous -- it is what a fresh install would have picked.
        cfg = a_config(name=None)
        found = core._relocated_core(cfg, [a_core()])
        self.assertEqual(found["host"], "192.168.50.119")

    def test_a_config_with_no_identity_refuses_an_ambiguous_lan(self):
        cfg = a_config(name=None)
        self.assertIsNone(core._relocated_core(
            cfg, [a_core(), a_core(host="192.168.50.150", name="other",
                                   unique_id="uid-other")]))

    def test_a_name_that_is_only_the_host_is_not_an_identity(self):
        # config.DEFAULTS lets `name` fall back to the address; that is a
        # label, not an identity, so it must not be matched against a Core's
        # real name.
        cfg = a_config(name="192.168.50.118")
        found = core._relocated_core(cfg, [a_core()])
        self.assertEqual(found["host"], "192.168.50.119")

    def test_nothing_on_the_network_is_not_a_relocation(self):
        self.assertIsNone(core._relocated_core(a_config(), []))


class _ConfigIsolated(unittest.TestCase):
    """Point config.py at a scratch dir, exactly as test_core.py does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        config.reset_paths()
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev
        config.reset_paths()


def _assume_lan_is_fine(testcase):
    """Pin the gateway probe so these tests do not depend on the host network.

    `_unreachable_status()` probes the default gateway to tell "the Core is
    off" apart from "this machine has no route". That probe is real network
    I/O, so any test reaching it inherits the runner's network: a gateway
    that answers gives `unreachable`, one that does not gives `no_network`.
    CI proved it -- eleven tests that pass on a home LAN failed on a runner
    whose gateway does not answer tcp/80.

    These tests are about the Core being unreachable, so they say so: the
    LAN is fine, and the fault is the Core's.
    """
    patcher = patch.object(core.net, "lan_reachable", lambda: True)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class TestStartFallsBackToDiscovery(_ConfigIsolated):
    """The startup half: a stored address that no longer answers."""
    def setUp(self):
        super().setUp()          # keeps config.py pointed at the scratch dir
        _assume_lan_is_fine(self)


    def test_a_stale_address_is_replaced_and_the_connect_retried(self):
        published = []
        session = core.RoonSession(published.append)
        session._cfg = a_config()
        api = types.SimpleNamespace(token=None, zones={},
                                    register_state_callback=lambda cb: None)
        attempts = []

        def connect(_self, _token):
            attempts.append(session._cfg["host"])
            return None if len(attempts) == 1 else api

        with unittest.mock.patch.object(core.RoonSession, "_connect", connect), \
             unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core()]):
            session.start()   # must NOT exit

        self.assertEqual(attempts, ["192.168.50.118", "192.168.50.119"])
        self.assertEqual(session.status, "ok")
        # Persisted, so the next start goes straight there.
        self.assertEqual(config.load()["host"], "192.168.50.119")
        # And the identity is recorded, so the next move matches exactly.
        self.assertEqual(config.load()["unique_id"], "uid-yavin")

    def test_discovery_finding_nothing_still_exits_1(self):
        published = []
        session = core.RoonSession(published.append)
        session._cfg = a_config()
        with unittest.mock.patch.object(core.RoonSession, "_connect",
                                        return_value=None), \
             unittest.mock.patch.object(core.sood, "discover", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                session.start()
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(published[-1]["status"], "unreachable")

    def test_a_relocation_that_cannot_connect_leaves_the_old_address_alone(self):
        """The address on disk is the last one known to WORK.

        Discovery is unauthenticated UDP; a stale or forged reply must not be
        able to cost the daemon an address that a Core has answered on. So an
        address is applied in memory, and written only once proven.
        """
        config.save(a_config())          # the known-good address
        session = core.RoonSession(lambda _p: None)
        session._cfg = config.load()
        with unittest.mock.patch.object(core.RoonSession, "_connect",
                                        return_value=None), \
             unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core(host="10.0.0.9")]):
            with self.assertRaises(SystemExit):
                session.start()
        self.assertEqual(config.load()["host"], "192.168.50.118")

    def test_first_run_discovery_is_not_persisted_until_a_core_answers(self):
        # Same rule on the path that has no stored address yet: a Core that
        # never completes a connection is not worth writing down.
        session = core.RoonSession(lambda _p: None)
        session._cfg = config.load()     # no host at all
        with unittest.mock.patch.object(core.RoonSession, "_connect",
                                        return_value=None), \
             unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core()]):
            with self.assertRaises(SystemExit):
                session.start()
        self.assertIsNone(config.load()["host"])

    def test_a_core_that_has_not_moved_is_not_reconnected_twice(self):
        # Discovery answering from the address we already failed on tells us
        # nothing new; a second identical connect attempt is pure delay.
        session = core.RoonSession(lambda _p: None)
        session._cfg = a_config(host="192.168.50.119")
        attempts = []

        def connect(_self, _token):
            attempts.append(1)
            return None

        with unittest.mock.patch.object(core.RoonSession, "_connect", connect), \
             unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core()]):
            with self.assertRaises(SystemExit):
                session.start()
        self.assertEqual(len(attempts), 1)


class _FakeSocket:
    def __init__(self, connected):
        self.connected = connected


class TestTheWatcherRelocates(_ConfigIsolated):
    """The running-daemon half: a connection that drops and never comes back.

    The watcher still does not exit on its own -- it exits only with positive
    evidence that the Core is somewhere else, which is the one thing roonapi's
    reconnect loop can never recover from.
    """
    def setUp(self):
        super().setUp()          # keeps config.py pointed at the scratch dir
        _assume_lan_is_fine(self)

    def _down_session(self):
        restarts = []
        s = core.RoonSession(lambda _p: None,
                             on_restart_needed=lambda: restarts.append(1))
        s._api = types.SimpleNamespace(_roonsocket=_FakeSocket(False), zones={})
        s._status = "ok"
        s._cfg = a_config()
        return s, restarts

    def test_a_sustained_outage_asks_the_network_where_the_core_went(self):
        s, restarts = self._down_session()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core()]) as discover:
            for _ in range(core.RELOCATE_SAMPLES):
                s._check_connection()
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(restarts, [1])
        # Deliberately NOT persisted here. The restarted process fails against
        # the address still on disk and relocates through `start()`, which
        # writes only what a Core actually answered on.
        self.assertIsNone(config.load()["host"])

    def test_a_short_outage_never_runs_discovery(self):
        s, restarts = self._down_session()
        with unittest.mock.patch.object(core.sood, "discover") as discover:
            for _ in range(core.RELOCATE_SAMPLES - 1):
                s._check_connection()
        discover.assert_not_called()
        self.assertEqual(restarts, [])
        self.assertEqual(s.status, "unreachable")

    def test_a_core_that_has_not_moved_keeps_waiting(self):
        s, restarts = self._down_session()
        s._cfg = a_config(host="192.168.50.119")
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core()]):
            for _ in range(core.RELOCATE_SAMPLES):
                s._check_connection()
        self.assertEqual(restarts, [])

    def test_a_silent_network_is_retried_on_the_next_window(self):
        # The Core may be down rather than moved. Asking once and never again
        # would leave a daemon that cannot recover from a move that happens
        # while it is already unreachable.
        s, _restarts = self._down_session()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[]) as discover:
            for _ in range(core.RELOCATE_SAMPLES * 2):
                s._check_connection()
        self.assertEqual(discover.call_count, 2)

    def test_recovery_resets_the_window(self):
        s, restarts = self._down_session()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core()]) as discover:
            for _ in range(core.RELOCATE_SAMPLES - 1):
                s._check_connection()
            s._api._roonsocket.connected = True
            s._check_connection()
            s._api._roonsocket.connected = False
            s._check_connection()
        discover.assert_not_called()
        self.assertEqual(restarts, [])

    def test_relocation_never_sweeps_the_lan(self):
        """The /24 sweep is 254 TCP connects (measured on a live LAN).

        Paid once on first run, with a human waiting, it is reasonable. Here it
        would fire on every restart for as long as a Core stayed switched off
        -- unsolicited scanning of someone else's network, on a loop, and the
        `MAX_SCAN_HOSTS` bound only limits one pass of it. A Core that MOVED is
        up and answering SOOD; one that answers nothing is off.
        """
        s, _restarts = self._down_session()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[]) as discover:
            for _ in range(core.RELOCATE_SAMPLES):
                s._check_connection()
        discover.assert_called_once_with(scan=False)

    def test_a_daemon_with_no_restart_hook_does_not_crash_the_watcher(self):
        # RoonSession is constructed without the hook in several tests and in
        # the CLI; a missing callback must degrade to "log and keep waiting".
        s = core.RoonSession(lambda _p: None)
        s._api = types.SimpleNamespace(_roonsocket=_FakeSocket(False), zones={})
        s._status = "ok"
        s._cfg = a_config()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[a_core()]):
            for _ in range(core.RELOCATE_SAMPLES):
                s._check_connection()
        self.assertEqual(s.status, "unreachable")


if __name__ == "__main__":
    unittest.main()
