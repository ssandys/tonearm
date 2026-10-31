"""Reconnect readiness: a cached zone map must not be routable.

Across a reconnect the vendored API keeps last session's zone map alive;
`snapshot()` refuses to publish it until `_zone_list_generation` reaches
`_required_zone_generation` (1 at start, raised after an observed outage). The bug
pinned down here: `selected_zone_id()` -- the browse target resolver -- and
`command()` still routed that stale map while status read "ok". Expected
fixed behavior: both refuse until the listing is fresh, local pin/unpin is
never blocked, and a fresh listing restores routing for explicit later
commands only, with no automatic call of any kind.

Everything is synthetic: a tiny RecordingApi plus config.load/save patches.
No networking, no fakes, no threads.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))
from tonearm_lib import core


class RecordingApi:
    """The transport surface the reconnect bug can reach, and nothing else."""

    def __init__(self, generation=0):
        # Real instances are seeded at zero and rise only on a full listing;
        # pass generation=None for a legacy double carrying no marker at all.
        if generation is not None:
            self._zone_list_generation = generation
        self.zones = {
            "zone-a": {"zone_id": "zone-a", "display_name": "Room A",
                       "state": "paused", "is_seek_allowed": True,
                       "outputs": [{"output_id": "output-1",
                                    "volume": {"type": "number", "value": 30,
                                               "min": 0, "max": 100}}]},
            "zone-b": {"zone_id": "zone-b", "display_name": "Room B",
                       "state": "playing",
                       "outputs": [{"output_id": "output-2",
                                    "volume": {"type": "number", "value": 10,
                                               "min": 0, "max": 100}}]},
        }
        self.calls = []

    def playback_control(self, zid, verb):
        self.calls.append(("playback", zid, verb))

    def seek(self, zid, pos, mode):
        self.calls.append(("seek", zid, pos, mode))

    def change_volume_raw(self, output, value, mode):
        self.calls.append(("volume", output, value, mode))

    def mute(self, output, flag):
        self.calls.append(("mute", output, flag))

    def transfer_zone(self, source, dest):
        self.calls.append(("transfer", source, dest))


REMOTE_VERBS = [("playpause", None), ("play", None), ("pause", None),
                ("next", None), ("previous", None), ("seek", 4),
                ("volume", 5), ("mute", None), ("unmute", None),
                ("transfer", "zone-b")]


class TestReconnectReadiness(unittest.TestCase):
    def session(self, api, pin="zone-a", strict=True):
        """Build a session around `api`; config is patched for the whole test."""
        self.saved = []
        for patcher in (
                patch.object(core.config, "load",
                             return_value={"pinned_zone_id": pin,
                                           "strict_pins": strict}),
                patch.object(core.config, "save",
                             lambda cfg: self.saved.append(dict(cfg)))):
            patcher.start()
            self.addCleanup(patcher.stop)
        s = core.RoonSession(lambda _: None)
        s._api, s._status = api, "ok"
        return s

    def test_observed_outage_waits_for_new_listing(self):
        api = RecordingApi(generation=1)
        api._roonsocket = type("Socket", (), {"connected": True})()
        s = self.session(api)
        self.assertEqual(s.selected_zone_id(), "zone-a")
        api._roonsocket.connected = False
        with patch.object(core, "_unreachable_status", return_value="unreachable"):
            for _ in range(core.DOWN_SAMPLES):
                s._check_connection()
        api._roonsocket.connected = True
        s._check_connection()
        self.assertEqual(s.snapshot()["selection"]["availability"], "unknown")
        for verb, arg in REMOTE_VERBS:
            s.command(verb, arg)
        self.assertEqual(api.calls, [])
        api._zone_list_generation += 1
        self.assertEqual(s.selected_zone_id(), "zone-a")
        self.assertEqual(api.calls, [])

    def test_browse_navigation_allowed_but_playback_refuses_stale_list(self):
        from tests.python.fakes import FakeRoon, yavin_levels
        from tonearm_lib import browse
        api = FakeRoon(yavin_levels())
        api.zones = RecordingApi().zones
        api._zone_list_generation = 1
        s = self.session(api)
        reply = s.browse("readiness", "search", term="oingo boingo")
        s._required_zone_generation = 2
        api.calls.clear()
        api.load_calls.clear()
        for op in ("play", "activate"):
            with self.assertRaises(browse.BrowseError) as caught:
                s.browse("readiness", op, index=0, level_id=reply["level_id"])
            self.assertEqual(caught.exception.token, "no_zone")
        self.assertEqual(api.calls + api.load_calls, [])
        self.assertTrue(s.browse("readiness", "search", term="oingo boingo")["ok"])

    def test_unknown_initial_generation_hides_zones_but_pin_unpin_works(self):
        api = RecordingApi(generation=0)  # required is 1: listing not fresh
        s = self.session(api)
        self.assertEqual(s.status, "ok")
        self.assertIsNone(s.snapshot()["zone"])
        self.assertEqual(s.snapshot()["zones"], [])
        self.assertIsNone(s.selected_zone_id())
        # Local selection is not a remote call: it must work while stale.
        s.command("zone", "zone-b")
        self.assertEqual(s._arbiter.pinned_id, "zone-b")
        self.assertEqual(self.saved[-1]["pinned_zone_id"], "zone-b")
        s.command("zone", "unpin")
        self.assertIsNone(s._arbiter.pinned_id)
        self.assertIsNone(self.saved[-1]["pinned_zone_id"])
        self.assertEqual(api.calls, [])

    def test_reconnect_below_required_generation_refuses_all_remote_verbs(self):
        api = RecordingApi(generation=1)
        s = self.session(api)
        s._required_zone_generation = 2  # post-reconnect bar, set by hand
        for verb, arg in REMOTE_VERBS:
            with self.subTest(verb=verb):
                s.command(verb, arg)
        self.assertEqual(api.calls, [])
        self.assertIsNone(s.selected_zone_id())
        self.assertIsNone(s.snapshot()["zone"])

    def test_fresh_generation_restores_zone_a_with_no_automatic_calls(self):
        api = RecordingApi(generation=1)
        s = self.session(api)
        s._required_zone_generation = 2
        api._zone_list_generation = 2
        snap = s.snapshot()
        self.assertEqual(snap["zone"]["id"], "zone-a")
        self.assertEqual({z["id"] for z in snap["zones"]},
                         {"zone-a", "zone-b"})
        for _ in range(3):
            self.assertEqual(s.selected_zone_id(), "zone-a")
        # A fresh listing routes nothing by itself.
        self.assertEqual(api.calls, [])
        s.command("pause")
        self.assertEqual(api.calls, [("playback", "zone-a", "pause")])

    def test_selected_zone_id_resolves_browse_target_across_repins(self):
        api = RecordingApi(generation=0)
        s = self.session(api)
        resolve = s.selected_zone_id  # the callable BrowseSession holds
        self.assertIsNone(resolve())
        s.command("zone", "zone-b")
        self.assertIsNone(resolve())  # a pin does not unblock a stale map
        api._zone_list_generation = 1
        self.assertEqual(resolve(), "zone-b")
        s.command("pause")
        self.assertEqual(api.calls, [("playback", "zone-b", "pause")])
        s.command("zone", "unpin")
        self.assertEqual(resolve(), "zone-b")  # auto-follow lands on playing
        s.command("playpause")
        self.assertEqual(api.calls[-1], ("playback", "zone-b", "playpause"))

    def test_selection_modes_resolve_only_after_the_fresh_listing(self):
        for strict, pin, expected in ((True, "zone-a", "zone-a"),
                                      (True, "zone-missing", None),
                                      (False, "zone-missing", "zone-b"),
                                      (False, None, "zone-b")):
            with self.subTest(strict=strict, pin=pin):
                api = RecordingApi(generation=1)
                s = self.session(api, pin=pin, strict=strict)
                s._required_zone_generation = 2
                self.assertIsNone(s.selected_zone_id())
                self.assertIsNone(s.snapshot()["zone"])
                api._zone_list_generation = 2
                self.assertEqual(s.selected_zone_id(), expected)
                self.assertEqual(api.calls, [])
                s.command("zone", "unpin")
                self.assertEqual(s.selected_zone_id(), "zone-b")
                self.assertIsNone(self.saved[-1]["pinned_zone_id"])

    def test_legacy_api_double_without_generation_defaults_to_one(self):
        api = RecordingApi(generation=None)  # old doubles carry no marker
        s = self.session(api)
        self.assertEqual(s.snapshot()["zone"]["id"], "zone-a")
        self.assertEqual(s.selected_zone_id(), "zone-a")
        s.command("seek", 12)
        s.command("volume", 7)
        s.command("mute")
        s.command("transfer", "zone-b")
        self.assertEqual(api.calls, [("seek", "zone-a", 12, "absolute"),
                                     ("volume", "output-1", 7, "absolute"),
                                     ("mute", "output-1", True),
                                     ("transfer", "zone-a", "zone-b")])
        self.assertEqual(s._arbiter.pinned_id, "zone-b")  # follow the music
        self.assertEqual(self.saved[-1]["pinned_zone_id"], "zone-b")


if __name__ == "__main__":
    unittest.main()
