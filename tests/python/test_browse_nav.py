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

    def test_an_omitted_level_id_is_stale_and_performs_no_action(self):
        # spec 5.1.1: index-addressed ops MUST carry level_id. A missing one
        # (the default, or None off a JSON payload) must not be treated as
        # "unchecked" -- that would let the guard above be skipped entirely.
        api, s, reply = searched()
        before = len(api.calls)
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(1)
        self.assertEqual(caught.exception.token, "stale")
        self.assertEqual(len(api.calls), before,
                         "an unenforceable request must not touch Roon at all")

    def test_an_unparseable_level_id_is_stale_not_a_bare_valueerror(self):
        # A malformed level_id must still carry the module's stable machine
        # token (spec 5.2), never leak int()'s ValueError to the caller.
        api, s, reply = searched()
        before = len(api.calls)
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(1, "abc")
        self.assertEqual(caught.exception.token, "stale")
        self.assertEqual(len(api.calls), before,
                         "an unenforceable request must not touch Roon at all")


class TestBack(unittest.TestCase):
    def test_works_with_no_level_id_argument_at_all(self):
        # Regression: back() does not address a row and never calls _check,
        # so it must not start requiring level_id.
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        out = s.back()
        self.assertTrue(out["ok"])
        self.assertEqual(out["path"], ["Search"])

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


class TestStaleCarriesTheLevel(unittest.TestCase):
    def test_a_stale_error_carries_the_level_so_the_widget_can_re_render(self):
        # spec 5.1.1: the daemon replies stale "including the current level so
        # the widget can re-render". BrowsePane._apply is already written
        # expecting it (`if (reply.rows !== undefined) _applyLevel(reply)`)
        # and a stale reply deliberately clears errorText -- so without the
        # payload the pane keeps rendering rows it can never act on, with
        # nothing on screen saying anything is wrong. Reachable exactly as
        # spec 7.4's "Daemon restarts" row describes.
        api, s, reply = searched()
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(1, reply["level_id"] + 99)
        level = caught.exception.level
        self.assertIsNotNone(level, "a stale error carried no level payload")
        self.assertEqual(level["level_id"], s.level_id)
        self.assertEqual(level["path"], ["Search"])
        self.assertEqual([r["title"] for r in level["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])


class LoadFailsOnce(FakeRoon):
    """FakeRoon whose Nth browse_load answers None.

    That is roonapi's own way of giving up: `_request` returns None after
    ~2.5s of retries. Local to this module rather than in fakes.py, which six
    test modules share.
    """

    def __init__(self, levels, fail_on):
        super().__init__(levels)
        self._loads = 0
        self._fail_on = fail_on

    def browse_load(self, opts):
        self._loads += 1
        if self._loads == self._fail_on:
            self.load_calls.append(dict(opts))
            return None
        return super().browse_load(opts)


# search() issues three loads (root, library, results); the first load of the
# following enter() is therefore the fourth.
ENTERS_LOAD = 4


class TestRoonErrorRecovery(unittest.TestCase):
    """spec 5.2/7.4: a Roon error resets the session to root and says so.

    Both halves of this were missing. enter() extended self._path BEFORE the
    load that can fail, and nothing compared Roon's own reported level against
    where the session believed it was -- so a single dropped load left the
    session describing a level it had never loaded, and the replayed keystroke
    then adopted the browse ROOT as if it were the row's contents, with
    ok: true and no error anywhere:

        enter #1 -> roon_error
        enter #2 ok. path = ['Search','Albums','Albums']
                    rows = ['Library']
                    level_id = 2   real position = root
    """

    def test_a_failed_load_does_not_advance_the_breadcrumb(self):
        api = LoadFailsOnce(yavin_levels(), ENTERS_LOAD)
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(1, reply["level_id"])
        self.assertEqual(caught.exception.token, "roon_error")
        # The session must still describe the level it actually holds rows
        # for, not the one it failed to load.
        self.assertEqual(s.current()["path"], ["Search"])
        self.assertEqual(s.current()["level_id"], reply["level_id"])
        self.assertEqual([r["title"] for r in s.current()["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_a_silent_root_reset_is_caught_and_resets_the_session(self):
        api = LoadFailsOnce(yavin_levels(), ENTERS_LOAD)
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")
        with self.assertRaises(browse.BrowseError):
            s.enter(1, reply["level_id"])
        # Roon is now genuinely inside "Albums" while the session still
        # describes "Search" -- and level_id still matches, so replaying the
        # same keystroke passes _check and browses a key that is now
        # positionally stale. Roon answers that with the ROOT and no error
        # (spec 2.5), which used to be adopted as the row's contents.
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(1, reply["level_id"])
        self.assertEqual(caught.exception.token, "roon_error")
        self.assertEqual(s.current()["path"], [])
        self.assertEqual(s.current()["rows"], [])
        self.assertEqual(caught.exception.level["rows"], [])

    def test_a_core_that_does_not_report_level_still_navigates(self):
        # The check must degrade to "no opinion" rather than making a Core
        # that omits `level` unbrowsable.
        levels = yavin_levels()
        for level in levels.values():
            level["list"].pop("level", None)
        api = FakeRoon(levels)
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")
        out = s.enter(1, reply["level_id"])
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])


if __name__ == "__main__":
    unittest.main()
