import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse, core
from fakes import FakeRoon, yavin_levels


class FakeSession(core.RoonSession):
    def __init__(self, api, status="ok"):
        super().__init__(lambda *a: None)
        self._api = api
        self._status = status


class TestBrowseSessionAccessor(unittest.TestCase):
    def test_returns_the_same_session_for_the_same_key(self):
        s = FakeSession(FakeRoon(yavin_levels()))
        self.assertIs(s.browse_session("widget"), s.browse_session("widget"))

    def test_different_keys_get_different_sessions(self):
        # spec 2.3: this is what keeps a future MCP server from disturbing
        # the widget's cursor.
        s = FakeSession(FakeRoon(yavin_levels()))
        self.assertIsNot(s.browse_session("widget"), s.browse_session("mcp"))

    def test_the_session_carries_its_key_to_roon(self):
        api = FakeRoon(yavin_levels())
        s = FakeSession(api)
        s.browse_session("mcp").search("oingo boingo")
        self.assertTrue(all(c.get("multi_session_key") == "mcp"
                            for c in api.calls))


class TestUnreachable(unittest.TestCase):
    def test_browse_is_refused_when_the_core_is_unreachable(self):
        # spec 7.4: reply immediately, never hang.
        api = FakeRoon(yavin_levels())
        s = FakeSession(api, status="unreachable")
        with self.assertRaises(browse.BrowseError) as caught:
            s.browse("widget", "search", term="oingo boingo")
        self.assertEqual(caught.exception.token, "unreachable")

    def test_no_roon_call_is_attempted_when_unreachable(self):
        api = FakeRoon(yavin_levels())
        s = FakeSession(api, status="unreachable")
        try:
            s.browse("widget", "search", term="oingo boingo")
        except browse.BrowseError:
            pass
        self.assertEqual(api.calls, [])


class TestDispatch(unittest.TestCase):
    def test_search_routes_through(self):
        s = FakeSession(FakeRoon(yavin_levels()))
        reply = s.browse("widget", "search", term="oingo boingo")
        self.assertEqual([r["title"] for r in reply["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_an_unknown_op_is_rejected(self):
        s = FakeSession(FakeRoon(yavin_levels()))
        with self.assertRaises(browse.BrowseError):
            s.browse("widget", "teleport")
