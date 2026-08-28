import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))
# tests/python is a package (has __init__.py), so unittest discover's
# top-level-dir sys.path insertion does not make sibling modules importable
# bare. Add this file's own directory so `import fakes` resolves the same
# way under `discover` as it would running this file directly.
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from tonearm_lib import browse
from fakes import FakeRoon, yavin_levels


def session():
    api = FakeRoon(yavin_levels())
    return api, browse.BrowseSession(api, "widget")


class TestSearch(unittest.TestCase):
    def test_returns_the_grouped_result_rows(self):
        api, s = session()
        reply = s.search("oingo boingo")
        self.assertTrue(reply["ok"])
        self.assertEqual([r["title"] for r in reply["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_reaches_search_by_input_prompt_not_by_title(self):
        # spec 11: the Library -> Search path is discovered, not hardcoded.
        levels = yavin_levels()
        levels["library"]["items"][0]["title"] = "Suche"
        api = FakeRoon(levels)
        s = browse.BrowseSession(api, "widget")
        self.assertTrue(s.search("oingo boingo")["ok"])

    def test_passes_the_term_as_input_alongside_the_item_key(self):
        # spec 2.1: the query rides with the Search item's item_key.
        api, s = session()
        s.search("oingo boingo")
        submit = [c for c in api.calls if "input" in c]
        self.assertEqual(len(submit), 1)
        self.assertEqual(submit[0]["input"], "oingo boingo")
        self.assertEqual(submit[0]["item_key"], "2:0")

    def test_every_call_carries_the_session_key(self):
        # spec 2.3: isolation depends on this being on EVERY call.
        api, s = session()
        s.search("oingo boingo")
        self.assertTrue(api.calls)
        for call in api.calls:
            self.assertEqual(call.get("multi_session_key"), "widget")
        for call in api.load_calls:
            self.assertEqual(call.get("multi_session_key"), "widget")

    def test_an_empty_search_returns_zero_rows_not_a_sentinel(self):
        # spec 2.7
        api, s = session()
        reply = s.search("nothing at all")
        self.assertEqual(reply["rows"], [])
        self.assertEqual(reply["count"], 0)

    def test_path_is_the_display_breadcrumb(self):
        api, s = session()
        self.assertEqual(s.search("oingo boingo")["path"], ["Search"])

    def test_no_reply_contains_an_item_key(self):
        # spec 5.3 invariant, asserted on a whole reply not just one row.
        api, s = session()
        self.assertNotIn("item_key", repr(s.search("oingo boingo")))


class TestLevelId(unittest.TestCase):
    def test_starts_at_zero_and_increments_on_each_level_change(self):
        api, s = session()
        self.assertEqual(s.level_id, 0)
        first = s.search("oingo boingo")["level_id"]
        self.assertEqual(first, 1)

    def test_two_searches_produce_different_level_ids(self):
        # spec 5.1.1: a reused id would let a stale index address a new level.
        api, s = session()
        a = s.search("oingo boingo")["level_id"]
        b = s.search("oingo boingo")["level_id"]
        self.assertNotEqual(a, b)
