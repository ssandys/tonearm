import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse
from fakes import FakeRoon, yavin_levels


def at_albums():
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget")
    reply = s.search("oingo boingo")
    reply = s.enter(1, reply["level_id"])      # Albums
    return api, s, reply


def at_tracks():
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget")
    reply = s.search("oingo boingo")
    reply = s.enter(2, reply["level_id"])      # Tracks
    return api, s, reply


def invoked_titles(api, levels):
    """Titles of every item_key the session browsed into."""
    seen = []
    for call in api.calls:
        key = call.get("item_key")
        if not key:
            continue
        for level in levels.values():
            for item in level["items"]:
                if item.get("item_key") == key:
                    seen.append(item["title"])
    return seen


class TestPlayAlbum(unittest.TestCase):
    def test_walks_two_levels_and_invokes_play_now(self):
        # spec 2.4: an album is 2 descents from its row to the action list.
        api, s, reply = at_albums()
        api.calls.clear()
        s.play(0, "play_now", reply["level_id"])
        titles = invoked_titles(api, yavin_levels())
        self.assertIn("Play Album", titles)
        self.assertIn("Play Now", titles)

    def test_queue_invokes_queue_not_play_now(self):
        api, s, reply = at_albums()
        api.calls.clear()
        s.play(0, "queue", reply["level_id"])
        titles = invoked_titles(api, yavin_levels())
        self.assertIn("Queue", titles)
        self.assertNotIn("Play Now", titles)

    def test_returns_to_the_level_the_user_was_on(self):
        # The user must not be teleported into the action list.
        api, s, reply = at_albums()
        out = s.play(0, "play_now", reply["level_id"])
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])

    def test_a_stale_level_id_plays_nothing(self):
        api, s, reply = at_albums()
        api.calls.clear()
        with self.assertRaises(browse.BrowseError) as caught:
            s.play(0, "play_now", reply["level_id"] + 99)
        self.assertEqual(caught.exception.token, "stale")
        self.assertEqual(api.calls, [])

    def test_an_unknown_action_is_rejected_before_touching_roon(self):
        api, s, reply = at_albums()
        api.calls.clear()
        with self.assertRaises(browse.BrowseError):
            s.play(0, "delete_everything", reply["level_id"])
        self.assertEqual(api.calls, [])


class TestPlayTrack(unittest.TestCase):
    def test_walks_one_level_for_a_track(self):
        # spec 2.4: a track is 1 descent, an album is 2. Same code path.
        api, s, reply = at_tracks()
        api.calls.clear()
        s.play(0, "play_now", reply["level_id"])
        self.assertIn("Play Now", invoked_titles(api, yavin_levels()))


class TestNoAction(unittest.TestCase):
    def test_a_row_with_no_reachable_action_reports_no_action(self):
        api, s, reply = at_albums()
        # "Nothing To Fear" has _goes_to None -- a dead end.
        with self.assertRaises(browse.BrowseError) as caught:
            s.play(1, "play_now", reply["level_id"])
        self.assertEqual(caught.exception.token, "no_action")

    def test_a_failed_play_leaves_the_user_where_they_were(self):
        api, s, reply = at_albums()
        try:
            s.play(1, "play_now", reply["level_id"])
        except browse.BrowseError:
            pass
        self.assertEqual(s.current()["path"], ["Search", "Albums"])


class TestActivate(unittest.TestCase):
    def test_plays_an_album(self):
        api, s, reply = at_albums()
        api.calls.clear()
        s.activate(0, reply["level_id"])
        self.assertIn("Play Now", invoked_titles(api, yavin_levels()))

    def test_reports_played_true_when_it_played(self):
        # The widget closes the popup on a play but not on a descend
        # (spec 7.3), and activate does both. Without this flag, Enter on a
        # category descends and instantly closes the popup.
        api, s, reply = at_albums()
        self.assertIs(s.activate(0, reply["level_id"])["played"], True)

    def test_descends_into_a_category_when_play_is_impossible(self):
        # spec 2.4: "Albums" and an album row are indistinguishable by hint,
        # so activate resolves it by trying, not by guessing.
        api = FakeRoon(yavin_levels())
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")
        out = s.activate(1, reply["level_id"])       # "Albums" category
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])

    def test_reports_played_false_when_it_descended(self):
        api = FakeRoon(yavin_levels())
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")
        self.assertIs(s.activate(1, reply["level_id"])["played"], False)


def deep_dead_end_levels():
    """A local fixture, NOT fakes.py (other tasks depend on that one
    unmodified). Structurally like yavin_levels() -- root -> library ->
    results -- but the one result row leads two real hops down
    ("hop1" -> "hop2") before dead-ending with no reachable action. Every
    branch in yavin_levels() itself dead-ends on the FIRST hop or finds an
    action within two, so it can never exercise a multi-hop dead end; this
    fixture exists solely to reach that path.
    """
    return {
        "root": {
            "list": {"title": "Explore", "count": 1, "level": 0},
            "items": [
                {"title": "Library", "item_key": "1:0", "hint": "list",
                 "image_key": None, "_goes_to": "library"},
            ],
        },
        "library": {
            "list": {"title": "Library", "count": 1, "level": 1},
            "items": [
                {"title": "Search", "item_key": "2:0", "hint": "list",
                 "image_key": None,
                 "input_prompt": {"prompt": "Search", "action": "Go"},
                 "_goes_to": lambda text: "results"},
            ],
        },
        "results": {
            "list": {"title": "Search", "count": 1, "level": 2},
            "items": [
                {"title": "Deep Dead End", "item_key": "9:0",
                 "image_key": None, "hint": "list", "_goes_to": "hop1"},
            ],
        },
        "hop1": {
            "list": {"title": "Deep Dead End", "count": 1, "level": 3},
            "items": [
                {"title": "Keep Going", "item_key": "9:1",
                 "hint": "action_list", "_goes_to": "hop2"},
            ],
        },
        "hop2": {
            "list": {"title": "Keep Going", "count": 1, "level": 4},
            "items": [
                {"title": "Still Nothing", "item_key": "9:2",
                 "hint": "action_list", "_goes_to": "dead_end"},
            ],
        },
        "dead_end": {
            "list": {"title": "Still Nothing", "count": 1, "level": 5},
            "items": [
                # hint "list", not "action_list" or "action": nothing here
                # continues the hunt and nothing here is itself playable.
                {"title": "Not An Action", "item_key": "9:3",
                 "hint": "list", "_goes_to": None},
            ],
        },
    }


class TestUnwindOnDeepDeadEnd(unittest.TestCase):
    def test_a_multi_hop_dead_end_unwinds_to_exactly_where_it_started(self):
        # The walk goes results -> hop1 -> hop2 -> dead_end (3 real descents:
        # the initial browse into "hop1", plus 2 more inside
        # _descend_to_action) before giving up. If _descend_to_action loses
        # track of descents already performed on its failure path, `play`
        # under-computes the unwind depth and `_unwind` pops too few levels,
        # leaving the fake (and, for real, Roon) two levels deeper than the
        # session believes -- silently, since the session's own path/rows
        # cache is never touched by play() and would not show it.
        api = FakeRoon(deep_dead_end_levels())
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")

        with self.assertRaises(browse.BrowseError) as caught:
            s.play(0, "play_now", reply["level_id"])
        self.assertEqual(caught.exception.token, "no_action")

        # The session's own cache was never touched -- true regardless of
        # whether the unwind was correct, since play() only mutates it on
        # success. Kept as a basic regression guard.
        self.assertEqual(s.current()["path"], ["Search"])

        # The real, load-bearing assertions: the fake's ACTUAL position, and
        # the pop_levels value play() actually issued, both have to reflect
        # the true depth reached (3), not merely len() of a value that a
        # failure path could discard.
        self.assertEqual(api.current, "results")
        pop_calls = [c["pop_levels"] for c in api.calls if "pop_levels" in c]
        self.assertEqual(pop_calls, [3])


if __name__ == "__main__":
    unittest.main()
