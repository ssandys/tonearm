"""Detecting a Roon connection that dies *after* the daemon is already up.

`start()` sets `_status` to "unreachable" only on its initial connect
attempts. Once it reached "ok", nothing ever set it back -- so a Core that
went down left the daemon reporting `ok` forever, with zone data quietly
going stale. The bar kept showing the last track as though nothing had
happened, which is the "confidently wrong" failure the whole severity design
exists to avoid.

The shape of the fix is dictated by roonapi: `_socket_watcher` is already a
reconnect loop (poll every 2s, and on `failed_state` rebuild the socket after
~21s, forever). So this code observes and reports; it must not exit and let
systemd restart it the way `start()` does, because recovery already happens
underneath.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import core   # noqa: E402


class FakeSocket:
    def __init__(self, connected=True):
        self.connected = connected


class FakeApi:
    """Only what `_check_connection` and `_zones` read."""

    def __init__(self, connected=True):
        self._roonsocket = FakeSocket(connected)
        self.zones = {
            "z1": {"zone_id": "z1", "display_name": "Kitchen", "state": "playing"},
        }


def session(api, status="ok"):
    published = []
    s = core.RoonSession(published.append)
    s._api = api
    s._status = status
    s._cfg = {"host": "192.168.50.118", "http_port": 9330, "name": "yavin"}
    return s, published


class TestDetectsTheDrop(unittest.TestCase):
    def test_a_sustained_drop_becomes_unreachable(self):
        api = FakeApi()
        s, published = session(api)
        api._roonsocket.connected = False
        s._check_connection()
        # One sample is not enough: roonapi's socket can close and reopen
        # inside a second, and flashing the bar red for that is noise.
        self.assertEqual(s.status, "ok")
        s._check_connection()
        self.assertEqual(s.status, "unreachable")
        self.assertTrue(published)
        self.assertEqual(published[-1]["status"], "unreachable")

    def test_a_single_sample_blip_never_reports_a_fault(self):
        api = FakeApi()
        s, published = session(api)
        api._roonsocket.connected = False
        s._check_connection()
        api._roonsocket.connected = True
        s._check_connection()
        self.assertEqual(s.status, "ok")
        self.assertEqual(published, [])

    def test_a_missing_socket_counts_as_down(self):
        # roonapi drops `_roonsocket` entirely in some teardown paths, and a
        # getattr default of None must not read as healthy.
        api = FakeApi()
        del api._roonsocket
        s, _ = session(api)
        s._check_connection()
        s._check_connection()
        self.assertEqual(s.status, "unreachable")


class TestRecovery(unittest.TestCase):
    def test_coming_back_returns_to_ok(self):
        api = FakeApi(connected=False)
        s, published = session(api)
        s._check_connection()
        s._check_connection()
        self.assertEqual(s.status, "unreachable")
        api._roonsocket.connected = True
        s._check_connection()
        self.assertEqual(s.status, "ok")
        self.assertEqual(published[-1]["status"], "ok")

    def test_a_REPLACED_socket_is_seen(self):
        # THE trap in this change. roonapi's reconnect does not revive the old
        # socket -- `_server_setup` builds a brand new RoonApiWebSocket and
        # rebinds `_roonsocket`. Code that captured the object once would keep
        # reading the dead one's `connected = False` and report unreachable
        # forever after the first successful reconnect.
        api = FakeApi(connected=False)
        s, _ = session(api)
        s._check_connection()
        s._check_connection()
        self.assertEqual(s.status, "unreachable")
        api._roonsocket = FakeSocket(connected=True)   # what roonapi actually does
        s._check_connection()
        self.assertEqual(s.status, "ok")

    def test_recovery_is_not_reported_twice(self):
        api = FakeApi()
        s, published = session(api)
        for _ in range(4):
            s._check_connection()
        self.assertEqual(published, [])


class TestOnlyTransitionsPublish(unittest.TestCase):
    def test_staying_down_publishes_once(self):
        api = FakeApi(connected=False)
        s, published = session(api)
        for _ in range(6):
            s._check_connection()
        self.assertEqual([p["status"] for p in published], ["unreachable"])

    def test_a_status_that_is_not_ok_yet_is_left_alone(self):
        # "connecting" and "unpaired" belong to start(); this watcher must not
        # promote a still-pairing daemon to "ok" just because a socket exists.
        api = FakeApi()
        s, published = session(api, status="unpaired")
        s._check_connection()
        self.assertEqual(s.status, "unpaired")
        self.assertEqual(published, [])


class TestSnapshotStopsClaimingAZone(unittest.TestCase):
    def test_an_unreachable_snapshot_carries_no_zone(self):
        # Otherwise the popup renders the stale track in its card while the
        # header directly above says "Roon Core unreachable" -- two halves of
        # one popup contradicting each other.
        api = FakeApi()
        s, _ = session(api)
        self.assertIsNotNone(s.snapshot()["zone"])
        api._roonsocket.connected = False
        s._check_connection()
        s._check_connection()
        snap = s.snapshot()
        self.assertEqual(snap["status"], "unreachable")
        self.assertIsNone(snap["zone"])
        self.assertEqual(snap["zones"], [])


if __name__ == "__main__":
    unittest.main()
