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
    # #11 replaced the gateway probe with a routing check; `routed_off_lan`
    # reports a FAULT, so "the LAN is fine" is False here rather than True.
    patcher = patch.object(core.net, "routed_off_lan",
                           lambda *_args, **_kw: False)
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

    def test_relocation_sweeps_the_lan_a_bounded_number_of_times(self):
        """The /24 sweep is 254 TCP connects (measured on a live LAN).

        This asserted `never` until #17. The reasoning was that a Core which
        MOVED is up and answering SOOD anyway, so the sweep bought nothing a
        loop of it could justify -- and that premise was measured false on a
        real network: multicast answered 0/7 there, including four 12s
        windows, while the sweep found the Core 3/6. Relocation simply cannot
        work on such a LAN without sweeping.

        What the original guard was really protecting is unchanged and still
        asserted here: the sweep must be BOUNDED, not fired on every window
        for as long as a Core stays switched off. `never` was one way to bound
        it; a backoff is another, and it is the one that leaves relocation
        working.
        """
        s, _restarts = self._down_session()
        windows = 20
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[]) as discover:
            for _ in range(core.RELOCATE_SAMPLES * windows):
                s._check_connection()
        self.assertEqual(discover.call_count, windows)
        swept = [c for c in discover.call_args_list if c.kwargs.get("scan")]
        # Windows 1-4, then every eighth: 12 and 20. Emphatically not 20 of 20.
        self.assertEqual(len(swept), 6, discover.call_args_list)

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


class TestExplainingARefusal(unittest.TestCase):
    """`_relocation_refusal` says why `_relocated_core` declined, for the log.

    The refusal itself is correct and this does not soften it (#15). What it
    fixes is that the daemon could see a Core on the LAN, decline to adopt it,
    and say nothing -- leaving "your Core is switched off" and "your Core is
    right there and I will not touch it" looking identical in the journal,
    when they are opposite problems with opposite fixes.

    Pure and separate from `_relocated_core` on purpose: the decision's
    contract and its tests above stay untouched.
    """

    def test_a_stored_id_matching_nothing_names_what_was_seen(self):
        # The live case that prompted this: config carried "uid-yavin", which
        # is not a Roon id at all, so no real Core could ever match it.
        cfg = a_config(unique_id="uid-yavin")
        note = core._relocation_note(cfg, [a_core(unique_id="96e11146-4bec")])
        self.assertIsNotNone(note)
        # It must name the Core that WAS found -- the address is the thing the
        # reader needs, and the whole complaint is that it went unsaid.
        self.assertIn("192.168.50.119", note)
        self.assertIn("yavin", note)

    def test_it_says_the_identity_is_what_did_not_match(self):
        cfg = a_config(unique_id="uid-yavin")
        note = core._relocation_note(cfg, [a_core(unique_id="96e11146-4bec")])
        self.assertIn("unique_id", note)

    def test_several_candidates_are_reported_as_ambiguous_not_as_absent(self):
        # Refusing because two Cores matched is a different fault from
        # refusing because none did, and the remedy differs.
        cfg = a_config(unique_id=None, name="yavin")
        note = core._relocation_note(
            cfg, [a_core(host="192.168.50.118"), a_core(host="192.168.50.120")])
        self.assertIsNotNone(note)
        self.assertIn("2", note)

    def test_nothing_discovered_is_reported_rather_than_assumed_to_mean_off(self):
        # This assertion was the other way round until it was measured. The
        # reasoning for silence was "nothing discovered means the Core is off,
        # which roonapi already reports" -- and on a live network that premise
        # is false: multicast SOOD never reaches this host over Wi-Fi, so
        # discovery returns nothing while the Core is up and answering a
        # unicast probe at a known address. Silence here is what left the
        # original incident with no signal at all.
        note = core._relocation_note(a_config(), [])
        self.assertIsNotNone(note)
        # Both readings have to be offered, because this cannot tell them
        # apart -- and naming only the likelier one sends the reader the wrong
        # way half the time.
        self.assertIn("no Cores", note)

    def test_a_core_already_at_the_configured_address_is_silent(self):
        # Not a relocation at all -- a Core rebooting, which roonapi's
        # reconnect handles. Nothing to report.
        cfg = a_config(host="192.168.50.119", unique_id="uid-yavin")
        self.assertIsNone(core._relocation_note(cfg, [a_core()]))

    def test_a_successful_relocation_is_not_a_refusal(self):
        # _find_relocated already logs that case itself; this must not
        # double-report it.
        cfg = a_config(unique_id="uid-yavin")
        self.assertIsNone(core._relocation_note(cfg, [a_core()]))


class TestRefusalsAreLoggedWithoutFloodingTheJournal(_ConfigIsolated):
    """`_find_relocated` runs on every restart and on every watcher poll.

    So a refusal that logged unconditionally would repeat for the whole length
    of an outage -- which is how a message that matters gets tuned out. It
    reports a conclusion when the conclusion CHANGES.
    """

    def _session_seeing(self, cores):
        session = core.RoonSession(lambda _payload: None)
        session._cfg = a_config(unique_id="uid-yavin")
        return session, unittest.mock.patch.object(
            core.sood, "discover", return_value=cores)

    def test_a_refusal_is_logged_once_not_once_per_poll(self):
        session, discovery = self._session_seeing(
            [a_core(unique_id="96e11146-4bec")])
        with discovery, self.assertLogs(core.LOG, level="WARNING") as logged:
            self.assertIsNone(session._find_relocated())
            self.assertIsNone(session._find_relocated())
            self.assertIsNone(session._find_relocated())
        self.assertEqual(len(logged.output), 1, logged.output)
        self.assertIn("192.168.50.119", logged.output[0])

    def test_a_changed_conclusion_is_reported_again(self):
        # The Core that could not be adopted has been replaced by a different
        # one at a different address. Same refusal, different facts -- and the
        # reader needs the new ones.
        session, discovery = self._session_seeing(
            [a_core(unique_id="96e11146-4bec")])
        with discovery, self.assertLogs(core.LOG, level="WARNING"):
            session._find_relocated()
        with unittest.mock.patch.object(
                core.sood, "discover",
                return_value=[a_core(host="192.168.50.121",
                                     unique_id="96e11146-4bec")]), \
             self.assertLogs(core.LOG, level="WARNING") as logged:
            session._find_relocated()
        self.assertEqual(len(logged.output), 1)
        self.assertIn("192.168.50.121", logged.output[0])

    def test_nothing_discovered_is_logged_once_not_for_the_whole_outage(self):
        # A Core switched off overnight is thousands of polls. One line.
        session, discovery = self._session_seeing([])
        with discovery, self.assertLogs(core.LOG, level="WARNING") as logged:
            for _ in range(5):
                self.assertIsNone(session._find_relocated())
        self.assertEqual(len(logged.output), 1, logged.output)


class TestWhenTheSweepIsWorthPaying(unittest.TestCase):
    """`_should_sweep` decides which relocation windows may fall back to the /24.

    Multicast never arrives on some networks -- measured 0/7 here, including
    four 12s windows, while the sweep found the Core 3/6 and a unicast probe
    to a known address answered 4/10 (#17). So relocation must be able to
    sweep, or it cannot work at all there.

    The sweep costs 254 connects and ~8s, and relocation is retried every
    RELOCATE_AFTER for as long as an outage lasts, so sweeping every window
    would hunt an absent Core all day. Early windows sweep anyway, because at
    roughly 50% per attempt one sweep is a coin flip on whether the bar ever
    heals; later ones back off.

    Deliberately NOT gated on the Core's last-known subnet. That was the first
    design and it could lock itself out: a router swap moves everything to a
    new /24, the stored address is then in no local subnet, no sweep is
    permitted, so the stored address is never refreshed and the gate never
    reopens. Same shape as a stale unique_id disabling relocation for good.
    Space is already bounded by _local_networks(), which scans private /24s
    only; this bounds it in time instead.
    """

    def test_the_first_windows_always_sweep(self):
        # ~94% cumulative at the measured hit rate, inside about 8 minutes.
        for n in (1, 2, 3, 4):
            self.assertTrue(core._should_sweep(n), "window %d" % n)

    def test_it_then_backs_off_rather_than_hunting_all_day(self):
        for n in range(5, 12):
            self.assertFalse(core._should_sweep(n), "window %d" % n)

    def test_it_keeps_trying_occasionally_so_a_late_return_is_found(self):
        # A Core that comes back at a new address hours later -- an overnight
        # router reboot -- must still be picked up without a restart.
        self.assertTrue(core._should_sweep(12))
        self.assertTrue(core._should_sweep(20))

    def test_a_zero_or_negative_window_never_sweeps(self):
        # Nothing asks at zero; a counter that drifts must not start sweeping.
        self.assertFalse(core._should_sweep(0))
        self.assertFalse(core._should_sweep(-1))


class TestTheSweepFallbackInPractice(_ConfigIsolated):
    """The wiring: which windows actually ask discovery to sweep."""

    def _session(self):
        session = core.RoonSession(lambda _payload: None)
        session._cfg = a_config(unique_id="uid-yavin")
        return session

    def test_an_early_window_asks_discovery_to_sweep(self):
        session = self._session()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[]) as discover:
            session._find_relocated()
        self.assertIs(discover.call_args.kwargs.get("scan"), True)

    def test_a_backed_off_window_stays_on_multicast_only(self):
        session = self._session()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[]) as discover:
            for _ in range(6):
                session._find_relocated()
        self.assertIs(discover.call_args.kwargs.get("scan"), False)

    def test_a_core_that_is_found_but_refused_still_backs_off(self):
        # The live #15 config: a stored unique_id no Core can match, so every
        # sweep SUCCEEDS at discovery and the adoption is then refused. Counting
        # empty discoveries would reset here and sweep every window forever, on
        # a fault no amount of sweeping can fix. What is counted is windows that
        # did not RESOLVE, whatever the reason.
        session = self._session()
        with unittest.mock.patch.object(
                core.sood, "discover",
                return_value=[a_core(unique_id="someone-else")]) as discover:
            for _ in range(6):
                self.assertIsNone(session._find_relocated())
        self.assertIs(discover.call_args.kwargs.get("scan"), False)

    def test_a_successful_relocation_resets_the_backoff(self):
        # An outage that ends and returns must get the aggressive windows
        # again, not resume mid-backoff from the previous one.
        session = self._session()
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[]):
            for _ in range(6):
                session._find_relocated()
        with unittest.mock.patch.object(
                core.sood, "discover",
                return_value=[a_core(unique_id="uid-yavin")]) as discover:
            session._find_relocated()
        self.assertEqual(session._unresolved_windows, 0)
        with unittest.mock.patch.object(core.sood, "discover",
                                        return_value=[]) as discover:
            session._find_relocated()
        self.assertIs(discover.call_args.kwargs.get("scan"), True)


class TestAFlappingDiscoveryDoesNotFlapTheJournal(_ConfigIsolated):
    """#15 logs on change; #17 made the conclusion unstable.

    The sweep is ~50% reliable on the network these were written for, so
    consecutive windows alternate between "found the Core, refused it" and
    "found nothing" -- and change-detection duly logged every flip, which is
    the repetition #15 set out to avoid, reintroduced by the interaction.

    Each distinct conclusion is worth saying once per outage. Saying it again
    because the coin landed the other way is not news.
    """

    def _session(self):
        s = core.RoonSession(lambda _p: None)
        s._cfg = a_config(host="192.168.50.199", unique_id="uid-yavin")
        return s

    def test_alternating_results_report_each_conclusion_once(self):
        session = self._session()
        seen = [[a_core(unique_id="someone-else")], [], [a_core(unique_id="someone-else")],
                [], [a_core(unique_id="someone-else")], []]
        with unittest.mock.patch.object(core.sood, "discover", side_effect=seen), \
             self.assertLogs(core.LOG, level="WARNING") as logged:
            for _ in seen:
                session._find_relocated()
        # One line for "found something I cannot adopt", one for "found
        # nothing" -- not six, and not four.
        self.assertEqual(len(logged.output), 2, logged.output)

    def test_a_genuinely_new_finding_is_still_reported(self):
        # Suppressing repeats must not suppress news: a Core appearing at a
        # different address is a fact the reader has not been given yet.
        session = self._session()
        seen = [[a_core(host="192.168.50.118", unique_id="someone-else")],
                [a_core(host="192.168.50.118", unique_id="someone-else")],
                [a_core(host="192.168.50.121", unique_id="someone-else")]]
        with unittest.mock.patch.object(core.sood, "discover", side_effect=seen), \
             self.assertLogs(core.LOG, level="WARNING") as logged:
            for _ in seen:
                session._find_relocated()
        self.assertEqual(len(logged.output), 2, logged.output)
        self.assertIn("192.168.50.121", logged.output[-1])

    def test_a_resolved_outage_lets_the_next_one_speak_again(self):
        session = self._session()
        with unittest.mock.patch.object(core.sood, "discover", return_value=[]), \
             self.assertLogs(core.LOG, level="WARNING"):
            session._find_relocated()
        with unittest.mock.patch.object(
                core.sood, "discover",
                return_value=[a_core(unique_id="uid-yavin")]):
            self.assertIsNotNone(session._find_relocated())
        with unittest.mock.patch.object(core.sood, "discover", return_value=[]), \
             self.assertLogs(core.LOG, level="WARNING") as logged:
            session._find_relocated()
        self.assertEqual(len(logged.output), 1, logged.output)
