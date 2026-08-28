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


def searched():
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget")
    reply = s.search("oingo boingo")
    return api, s, reply


class TestEnter(unittest.TestCase):
    def test_descends_into_the_addressed_row(self):
        api, s, reply = searched()
        out = s.enter(1, reply["level_id"])          # "Albums"
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])

    def test_extends_the_breadcrumb(self):
        api, s, reply = searched()
        self.assertEqual(s.enter(1, reply["level_id"])["path"],
                         ["Search", "Albums"])

    def test_a_mismatched_level_id_is_stale_and_performs_no_action(self):
        # spec 5.1.1: THE guard against playing the wrong album. The index is
        # still valid; it just means something else now.
        api, s, reply = searched()
        before = len(api.calls)
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(1, reply["level_id"] + 99)
        self.assertEqual(caught.exception.token, "stale")
        self.assertEqual(len(api.calls), before,
                         "a stale request must not touch Roon at all")

    def test_an_out_of_range_index_is_bad_index(self):
        api, s, reply = searched()
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(99, reply["level_id"])
        self.assertEqual(caught.exception.token, "bad_index")


class TestBack(unittest.TestCase):
    def test_uses_pop_levels_and_never_re_walks_from_root(self):
        # spec 2.5: re-walking invalidates every captured key, and a stale key
        # silently returns the ROOT with no error.
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        api.calls.clear()
        s.back()
        self.assertTrue(any("pop_levels" in c for c in api.calls))
        self.assertFalse(any(c.get("pop_all") for c in api.calls))

    def test_returns_to_the_previous_level(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        self.assertEqual([r["title"] for r in s.back()["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_shortens_the_breadcrumb(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        self.assertEqual(s.back()["path"], ["Search"])

    def test_back_at_the_top_is_a_no_op_that_still_returns_the_level(self):
        api, s, reply = searched()
        out = s.back()
        self.assertTrue(out["ok"])
        self.assertEqual(out["path"], ["Search"])


class TestPage(unittest.TestCase):
    def test_loads_at_the_requested_offset(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        api.load_calls.clear()
        s.page(100)
        self.assertEqual(api.load_calls[-1]["offset"], 100)

    def test_reports_the_new_offset(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        self.assertEqual(s.page(100)["offset"], 100)


class TestReset(unittest.TestCase):
    def test_clears_the_stack_and_the_rows(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        out = s.reset()
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["path"], [])


if __name__ == "__main__":
    unittest.main()
