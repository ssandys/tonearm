"""Selection safety, using fabricated zones and existing browse fakes only."""
import copy
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))
from tests.python.fakes import FakeRoon, yavin_levels
from tonearm_lib import core, browse


KITCHEN = "zone-kitchen"
DEN = "zone-den"


def fake_api(playing=None):
    api = FakeRoon(yavin_levels())
    api.zones = {
        zid: {"zone_id": zid, "display_name": name,
              "state": "playing" if playing == zid else "paused"}
        for zid, name in ((KITCHEN, "Kitchen"), (DEN, "Den"))
    }
    return api


class RecordingApi:
    """Only the transport surface these safety tests exercise."""
    def __init__(self):
        self.zones = {
            "zone-a": {"zone_id": "zone-a", "display_name": "Fixed room",
                       "state": "paused",
                       "outputs": [{"output_id": "output-a",
                                    "volume": {"type": "fixed"}}]},
            "zone-b": {"zone_id": "zone-b", "display_name": "Adjustable room",
                       "state": "playing",
                       "outputs": [{"output_id": "output-b",
                                    "volume": {"type": "number", "value": 10,
                                               "min": 0, "max": 100}}]},
        }
        self.calls = []

    def playback_control(self, zid, verb):
        self.calls.append(("transport", zid, verb))

    def change_volume_raw(self, output, value, mode):
        self.calls.append(("volume", output, value, mode))


class TestSelectionSafety(unittest.TestCase):
    def session(self, api, strict=True):
        with patch.object(core.config, "load", return_value={
                "pinned_zone_id": "zone-a", "strict_pins": strict}):
            s = core.RoonSession(lambda _: None)
        s._api, s._status = api, "ok"
        return s

    def test_missing_pin_leaves_other_room_visible_but_not_selected(self):
        api = RecordingApi()
        s = self.session(api)
        del api.zones["zone-a"]
        snap = s.snapshot()
        self.assertIsNone(snap["zone"])
        self.assertEqual([z["id"] for z in snap["zones"]], ["zone-b"])
        self.assertIsNone(s.selected_zone_id())
        self.assertEqual(s._arbiter.pinned_id, "zone-a")

    def test_missing_pin_refuses_all_targeted_commands(self):
        api = RecordingApi()
        s = self.session(api)
        del api.zones["zone-a"]
        for verb, arg in [("play", None), ("pause", None), ("playpause", None),
                          ("next", None), ("previous", None), ("seek", 4),
                          ("volume", 5), ("mute", None), ("unmute", None),
                          ("transfer", "zone-b")]:
            s.command(verb, arg)
        self.assertEqual(api.calls, [])

    def test_return_restores_selection_without_sending_commands(self):
        api = RecordingApi()
        s = self.session(api)
        original = copy.deepcopy(api.zones.pop("zone-a"))
        self.assertIsNone(s.snapshot()["zone"])
        api.zones["zone-a"] = original
        self.assertEqual(s.snapshot()["zone"]["id"], "zone-a")
        self.assertEqual(api.calls, [])

    def test_unpin_restores_existing_auto_follow(self):
        s = self.session(RecordingApi())
        s._arbiter.unpin()
        self.assertEqual(s.selected_zone_id(), "zone-b")
        s.command("pause")
        self.assertEqual(s._api.calls, [("transport", "zone-b", "pause")])

    def test_legacy_mode_keeps_missing_pin_fallback(self):
        s = self.session(RecordingApi(), strict=False)
        del s._api.zones["zone-a"]
        self.assertEqual(s.selected_zone_id(), "zone-b")

    def test_disconnected_states_refuse_cached_commands_in_both_modes(self):
        for strict in (False, True):
            for status in ("connecting", "unpaired", "unreachable", "no_network"):
                with self.subTest(strict=strict, status=status):
                    s = self.session(RecordingApi(), strict)
                    s._status = status
                    for verb, arg in [("play", None), ("pause", None),
                                      ("volume", 5), ("transfer", "zone-b")]:
                        s.command(verb, arg)
                    self.assertEqual(s._api.calls, [])
                    self.assertIsNone(s.selected_zone_id())

    def test_missing_pin_browse_navigation_works_but_play_and_activate_refuse(self):
        api = fake_api(playing=DEN)
        s = self.session(api)
        s._arbiter.pin(KITCHEN)
        reply = s.browse("test", "search", term="oingo boingo")
        del api.zones[KITCHEN]
        api.calls.clear()
        api.load_calls.clear()
        for op in ("play", "activate"):
            with self.assertRaises(browse.BrowseError) as caught:
                s.browse("test", op, index=0, level_id=reply["level_id"])
            self.assertEqual(caught.exception.token, "no_zone")
        self.assertEqual(api.calls + api.load_calls, [])
        self.assertTrue(s.browse("test", "search", term="oingo boingo")["ok"])

    def test_disconnected_browse_refuses_before_api_call(self):
        s = self.session(fake_api(playing=DEN))
        s._status = "unreachable"
        with self.assertRaises(browse.BrowseError) as caught:
            s.browse("test", "play", index=0)
        self.assertEqual(caught.exception.token, "unreachable")
        self.assertEqual(s._api.calls, [])

    def test_pin_disappears_during_browse_play_no_later_call_is_untargeted(self):
        api = fake_api(playing=DEN)
        s = self.session(api)
        s._arbiter.pin(KITCHEN)
        reply = s.browse("test", "search", term="oingo boingo")
        api.calls.clear()
        api.load_calls.clear()
        original = api.browse_browse
        def disappear(opts):
            result = original(opts)
            api.zones.pop(KITCHEN, None)
            return result
        api.browse_browse = disappear
        with self.assertRaises(browse.BrowseError) as caught:
            s.browse("test", "play", index=0, level_id=reply["level_id"])
        self.assertEqual(caught.exception.token, "no_zone")
        self.assertTrue(api.calls)
        self.assertTrue(all(c.get("zone_or_output_id") == KITCHEN
                            for c in api.calls + api.load_calls))
        self.assertTrue(s.browse("test", "search", term="oingo boingo")["ok"])
