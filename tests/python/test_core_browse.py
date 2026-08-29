import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse, core
from fakes import FakeRoon, yavin_levels

# Roon's own zone shape, only as far as state.normalize_zone reads it. Built
# here rather than in fakes.py (six modules share that one) and given values
# nothing in core.py could produce by accident: the id is not a default
# anywhere, and "playing" is what makes the arbiter's active branch -- not its
# last-followed fallback -- do the selecting.
KITCHEN = "16018f1b-kitchen"
DEN = "16018f1b-den"


def fake_api(levels=None, playing=None):
    """A FakeRoon that also answers `.zones`, the way RoonApi does.

    RoonSession reads `self._api.zones` for zone arbitration, and browse now
    depends on that arbitration (every browse call must carry the followed
    zone's id). FakeRoon models only the browse surface, so the zone dict is
    attached here, per-test, instead of widening the shared fake.
    """
    api = FakeRoon(levels if levels is not None else yavin_levels())
    api.zones = {
        KITCHEN: {"zone_id": KITCHEN, "display_name": "Kitchen",
                  "state": "playing" if playing == KITCHEN else "paused"},
        DEN: {"zone_id": DEN, "display_name": "Den",
              "state": "playing" if playing == DEN else "paused"},
    }
    return api


class FakeSession(core.RoonSession):
    def __init__(self, api, status="ok"):
        super().__init__(lambda *a: None)
        self._api = api
        self._status = status


class TestBrowseSessionAccessor(unittest.TestCase):
    def test_returns_the_same_session_for_the_same_key(self):
        s = FakeSession(fake_api())
        self.assertIs(s.browse_session("widget"), s.browse_session("widget"))

    def test_different_keys_get_different_sessions(self):
        # spec 2.3: this is what keeps a future MCP server from disturbing
        # the widget's cursor.
        s = FakeSession(fake_api())
        self.assertIsNot(s.browse_session("widget"), s.browse_session("mcp"))

    def test_the_session_carries_its_key_to_roon(self):
        api = fake_api()
        s = FakeSession(api)
        s.browse_session("mcp").search("oingo boingo")
        self.assertTrue(all(c.get("multi_session_key") == "mcp"
                            for c in api.calls))


class TestUnreachable(unittest.TestCase):
    def test_browse_is_refused_when_the_core_is_unreachable(self):
        # spec 7.4: reply immediately, never hang.
        api = fake_api()
        s = FakeSession(api, status="unreachable")
        with self.assertRaises(browse.BrowseError) as caught:
            s.browse("widget", "search", term="oingo boingo")
        self.assertEqual(caught.exception.token, "unreachable")

    def test_no_roon_call_is_attempted_when_unreachable(self):
        api = fake_api()
        s = FakeSession(api, status="unreachable")
        try:
            s.browse("widget", "search", term="oingo boingo")
        except browse.BrowseError:
            pass
        self.assertEqual(api.calls, [])


class TestDispatch(unittest.TestCase):
    def test_search_routes_through(self):
        s = FakeSession(fake_api())
        reply = s.browse("widget", "search", term="oingo boingo")
        self.assertEqual([r["title"] for r in reply["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_an_unknown_op_is_rejected(self):
        s = FakeSession(fake_api())
        with self.assertRaises(browse.BrowseError):
            s.browse("widget", "teleport")


class TestZoneWiring(unittest.TestCase):
    """The daemon's followed zone reaches Roon's browse opts.

    Without `zone_or_output_id`, a browse action succeeds at the protocol
    level and plays into nothing (measured). The zone must be the same one
    the bar and the transport commands already follow -- the widget never
    picks it (spec 3), so this wiring is the only thing that supplies it.
    """

    def test_browse_calls_target_the_followed_zone(self):
        api = fake_api(playing=DEN)
        s = FakeSession(api)
        s.browse("widget", "search", term="oingo boingo")
        self.assertTrue(api.calls)
        for call in api.calls + api.load_calls:
            self.assertEqual(call.get("zone_or_output_id"), DEN)

    def test_a_repin_between_browses_moves_the_target(self):
        # The session is created once and reused, so a captured-at-
        # construction zone would keep aiming at the old room forever.
        # _arbiter.pin directly rather than command("zone", ...): that path
        # writes the real user config file.
        api = fake_api(playing=DEN)
        s = FakeSession(api)
        s.browse("widget", "search", term="oingo boingo")
        s._arbiter.pin(KITCHEN)
        api.calls.clear()
        api.load_calls.clear()
        s.browse("widget", "search", term="oingo boingo")
        self.assertTrue(api.calls)
        for call in api.calls + api.load_calls:
            self.assertEqual(call.get("zone_or_output_id"), KITCHEN)

    def test_with_no_zone_at_all_navigation_works_and_a_play_refuses(self):
        api = fake_api()
        api.zones = {}
        s = FakeSession(api)
        reply = s.browse("widget", "search", term="oingo boingo")
        self.assertTrue(reply["ok"])
        with self.assertRaises(browse.BrowseError) as caught:
            s.browse("widget", "play", index=1, level_id=reply["level_id"])
        self.assertEqual(caught.exception.token, "no_zone")
