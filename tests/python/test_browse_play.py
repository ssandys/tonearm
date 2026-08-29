import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse
from fakes import FakeRoon, yavin_levels

# A value the implementation could not produce by accident (AGENTS.md's
# "a fixture whose value coincides with a default proves nothing"): it is
# neither a Roon item_key shape nor anything else this module constructs.
ZONE = "zone-kitchen-7f3a"


class ZoneHolder:
    """A mutable stand-in for the daemon's followed-zone lookup.

    Every play/activate path requires one: without a zone_or_output_id in the
    browse opts, Roon accepts the action and plays into nothing (measured), so
    play() now refuses rather than reporting success. A holder rather than a
    lambda so a test can repin between two calls and see the change followed.
    """

    def __init__(self, zone_id=ZONE):
        self.zone_id = zone_id

    def __call__(self):
        return self.zone_id


def at_albums(zone=None):
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget", zone or ZoneHolder())
    reply = s.search("oingo boingo")
    reply = s.enter(1, reply["level_id"])      # Albums
    return api, s, reply


def at_tracks():
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget", ZoneHolder())
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
        api.calls.clear()
        out = s.play(0, "play_now", reply["level_id"])
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])
        # The two asserts above read s._path/s._rows, which play() never
        # mutates on ANY outcome -- they would pass just as well with
        # self._unwind(depth) deleted from play() entirely. The real,
        # discriminating check is the depth _unwind actually issued: an
        # album is 2 descents (album_detail, then album_actions) plus the
        # initial browse into the row, so pop_levels must be exactly 3 --
        # not merely present, since a wrong depth is the exact bug fixed
        # in _descend_to_action's out-param change (see
        # TestUnwindOnDeepDeadEnd). Checked via pop_levels rather than
        # api.current: invoking the terminal "Play Now" action item itself
        # resets the fake to root (action items carry no _goes_to in
        # yavin_levels()), before _unwind ever runs -- so api.current is
        # "root" regardless of whether the unwind depth was right, and
        # pop_levels is the only ground truth left to check.
        pop_calls = [c["pop_levels"] for c in api.calls if "pop_levels" in c]
        self.assertEqual(pop_calls, [3])

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
        # Ground truth on the unwind depth, same reasoning as the album
        # test above: a track's row IS its own action list (1 descent),
        # so the initial browse already reaches it and only the invoke
        # itself follows -- pop_levels must be exactly 2, not merely
        # present.
        pop_calls = [c["pop_levels"] for c in api.calls if "pop_levels" in c]
        self.assertEqual(pop_calls, [2])


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
        s = browse.BrowseSession(api, "widget", ZoneHolder())
        reply = s.search("oingo boingo")
        out = s.activate(1, reply["level_id"])       # "Albums" category
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])

    def test_reports_played_false_when_it_descended(self):
        api = FakeRoon(yavin_levels())
        s = browse.BrowseSession(api, "widget", ZoneHolder())
        reply = s.search("oingo boingo")
        self.assertIs(s.activate(1, reply["level_id"])["played"], False)


class TestZoneTargeting(unittest.TestCase):
    """A browse action with no `zone_or_output_id` plays into nothing.

    Measured live against the Core, same album, same code path, only this
    field differing:

        [nozone]   invoking Play Now -> zone state: paused  | Insanity
        [withzone] invoking Play Now -> zone state: playing | Speak to Me

    Roon reports success either way, so nothing above this layer can notice.
    """

    def test_every_browse_and_load_call_of_a_play_carries_the_zone(self):
        api, s, reply = at_albums()
        api.calls.clear()
        api.load_calls.clear()
        s.play(0, "play_now", reply["level_id"])
        self.assertTrue(api.calls, "the play made no browse calls at all")
        self.assertTrue(api.load_calls, "the play made no load calls at all")
        for call in api.calls + api.load_calls:
            self.assertEqual(call.get("zone_or_output_id"), ZONE)

    def test_search_and_enter_carry_the_zone_too(self):
        # The vendored library puts it in play_media's browse AND load opts
        # (roonapi.py:590-595). It rides on _opts(), so every call gets it,
        # not just the one that invokes an action.
        api = FakeRoon(yavin_levels())
        s = browse.BrowseSession(api, "widget", ZoneHolder())
        reply = s.search("oingo boingo")
        s.enter(1, reply["level_id"])
        self.assertTrue(api.calls)
        for call in api.calls + api.load_calls:
            self.assertEqual(call.get("zone_or_output_id"), ZONE)

    def test_play_with_no_zone_raises_rather_than_reporting_success(self):
        # The whole point: `played: true` for silence is the failure this
        # guard exists to prevent. It must also cost no Roon round-trip.
        api, s, reply = at_albums(zone=ZoneHolder(None))
        api.calls.clear()
        with self.assertRaises(browse.BrowseError) as caught:
            s.play(0, "play_now", reply["level_id"])
        self.assertEqual(caught.exception.token, "no_zone")
        self.assertEqual(api.calls, [])

    def test_activate_with_no_zone_raises_rather_than_descending(self):
        # activate falls back to a descend ONLY on no_action. Descending into
        # the album instead would look like the widget did something sensible
        # while no music started and nothing said why.
        api, s, reply = at_albums(zone=ZoneHolder(None))
        with self.assertRaises(browse.BrowseError) as caught:
            s.activate(0, reply["level_id"])
        self.assertEqual(caught.exception.token, "no_zone")

    def test_navigation_still_works_with_no_zone_and_omits_the_field(self):
        # Browsing with no zone selected is meaningful; only playing is not.
        # Omitted, never sent as null: an explicit null for a field Roon
        # expects to hold an id is a different request from not sending it.
        api = FakeRoon(yavin_levels())
        s = browse.BrowseSession(api, "widget", ZoneHolder(None))
        reply = s.search("oingo boingo")
        reply = s.enter(1, reply["level_id"])
        self.assertEqual([r["title"] for r in reply["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])
        self.assertEqual(s.back()["path"], ["Search"])
        self.assertEqual(s.page(0)["offset"], 0)
        for call in api.calls + api.load_calls:
            self.assertNotIn("zone_or_output_id", call)

    def test_the_zone_is_read_at_call_time_so_a_repin_is_followed(self):
        # The user can repin between two browses. A zone captured once at
        # construction would send this play to the room they just left.
        holder = ZoneHolder()
        api, s, reply = at_albums(zone=holder)
        holder.zone_id = "zone-study-04d1"
        api.calls.clear()
        api.load_calls.clear()
        s.play(0, "play_now", reply["level_id"])
        self.assertTrue(api.calls)
        for call in api.calls + api.load_calls:
            self.assertEqual(call.get("zone_or_output_id"), "zone-study-04d1")


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
            # TWO items, deliberately: after R12's single-item wrapper rule
            # (fix round 3), a lone "list" item at a level IS followed, on
            # the same measured basis that distinguishes a wrapper from a
            # category. A genuine dead end must therefore have more than one
            # item too, or it stops being a dead end under the new rule and
            # this fixture would no longer test what it says it tests.
            "list": {"title": "Still Nothing", "count": 2, "level": 5},
            "items": [
                # hint "list", not "action_list" or "action": nothing here
                # continues the hunt and nothing here is itself playable.
                {"title": "Not An Action", "item_key": "9:3",
                 "hint": "list", "_goes_to": None},
                {"title": "Also Not An Action", "item_key": "9:4",
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
        s = browse.BrowseSession(api, "widget", ZoneHolder())
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


def deep_wrapper_levels():
    """A local fixture (not fakes.py) reproducing the shape MEASURED live
    against a real Roon Core (R12 fix round 3), which neither yavin_levels()
    nor deep_dead_end_levels() modelled -- exactly why 13 passing tests still
    shipped a broken "play an album" feature:

        1. album row in the Albums list        hint: "list"
        2. -> WRAPPER level, count == 1         hint: "list" (its one item)
        3. -> album contents, count == 10       "Play Album" (action_list)
                                                  + 9 track rows (action_list)
        4. -> "Play Album" actions               Play Now, Add Next, Queue,
                                                  Start Radio

    "results" also carries an "Albums" CATEGORY row (many items, none
    action_list) alongside the album row, so the R12 regression guard --
    a category must still dead-end rather than wander into its first
    child -- can be verified against this same, more realistic shape.
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
            "list": {"title": "Search", "count": 2, "level": 2},
            "items": [
                {"title": "Wrapped Album", "item_key": "8:0",
                 "image_key": "abcd", "hint": "list", "_goes_to": "wrapper"},
                {"title": "Albums", "item_key": "8:1",
                 "image_key": None, "hint": "list", "_goes_to": "category"},
            ],
        },
        "wrapper": {
            # count == 1, ONE keyed item, hint "list" -- the measured shape
            # that a plain "action_list only" continuation cannot cross.
            "list": {"title": "Wrapped Album", "count": 1, "level": 3},
            "items": [
                {"title": "Wrapped Album", "item_key": "8:2",
                 "hint": "list", "_goes_to": "contents"},
            ],
        },
        "contents": {
            "list": {"title": "Wrapped Album", "count": 10, "level": 4},
            "items": [
                {"title": "Play Album", "item_key": "8:3",
                 "hint": "action_list", "_goes_to": "play_album_actions"},
            ] + [
                {"title": "Track %d" % n, "item_key": "8:%d" % (10 + n),
                 "hint": "action_list", "_goes_to": "track_actions"}
                for n in range(1, 10)
            ],
        },
        "play_album_actions": {
            "list": {"title": "Play Album", "count": 4, "level": 5,
                     "hint": "action_list"},
            "items": [
                {"title": "Play Now", "item_key": "8:20", "hint": "action"},
                {"title": "Add Next", "item_key": "8:21", "hint": "action"},
                {"title": "Queue", "item_key": "8:22", "hint": "action"},
                {"title": "Start Radio", "item_key": "8:23", "hint": "action"},
            ],
        },
        "category": {
            # Multi-item (3, standing in for the measured 21), hint "list",
            # NO action_list anywhere -- the R12 regression guard. Must
            # still dead-end, never wander into "Album One".
            "list": {"title": "Albums", "count": 3, "level": 3},
            "items": [
                {"title": "Album One", "item_key": "8:30",
                 "hint": "list", "_goes_to": None},
                {"title": "Album Two", "item_key": "8:31",
                 "hint": "list", "_goes_to": None},
                {"title": "Album Three", "item_key": "8:32",
                 "hint": "list", "_goes_to": None},
            ],
        },
    }


class TestWrapperLevel(unittest.TestCase):
    def test_play_reaches_play_now_through_the_single_item_wrapper(self):
        api = FakeRoon(deep_wrapper_levels())
        s = browse.BrowseSession(api, "widget", ZoneHolder())
        reply = s.search("oingo boingo")
        api.calls.clear()
        out = s.play(0, "play_now", reply["level_id"])  # "Wrapped Album"
        self.assertIs(out["played"], True)
        self.assertIn("Play Now", invoked_titles(api, deep_wrapper_levels()))
        # Ground truth: initial browse into the row (1) + wrapper->contents
        # (2) + contents->"Play Album" (3) + "Play Album"->Play Now (4) = 4.
        # Not merely "some pop_levels happened" -- a wrong depth is the
        # exact bug fixed in round 1, and this is the deeper path it must
        # still get right.
        pop_calls = [c["pop_levels"] for c in api.calls if "pop_levels" in c]
        self.assertEqual(pop_calls, [4])

    def test_activate_plays_the_album_rather_than_descending(self):
        api = FakeRoon(deep_wrapper_levels())
        s = browse.BrowseSession(api, "widget", ZoneHolder())
        reply = s.search("oingo boingo")
        out = s.activate(0, reply["level_id"])  # "Wrapped Album"
        self.assertIs(out["played"], True)

    def test_activate_still_descends_a_multi_item_category_with_no_action_list(self):
        # The R12 regression guard: this must not weaken. A category (here,
        # 3 items, none action_list -- standing in for the measured 21) has
        # no single keyed item for the new fallback rule to catch, so it
        # must still dead-end and activate must still fall back to enter().
        api = FakeRoon(deep_wrapper_levels())
        s = browse.BrowseSession(api, "widget", ZoneHolder())
        reply = s.search("oingo boingo")
        out = s.activate(1, reply["level_id"])  # "Albums" category
        self.assertIs(out["played"], False)
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Album One", "Album Two", "Album Three"])


def double_wrapper_levels():
    """SYNTHETIC -- not measured against a real Core, unlike
    deep_wrapper_levels() above. Exists solely to prove MAX_ACTION_DEPTH's
    margin actually functions: one MORE single-item wrapper than the real,
    measured shape, so reaching Play Now takes 4 real descents inside
    _descend_to_action's loop (5 total with the initial browse) rather than
    3. This is exactly the "one more wrapper level anywhere in Roon's
    hierarchy" scenario the raised depth exists to survive.
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
                {"title": "Double Wrapped Album", "item_key": "7:0",
                 "image_key": "abcd", "hint": "list", "_goes_to": "wrapper1"},
            ],
        },
        "wrapper1": {
            "list": {"title": "Double Wrapped Album", "count": 1, "level": 3},
            "items": [
                {"title": "Double Wrapped Album", "item_key": "7:1",
                 "hint": "list", "_goes_to": "wrapper2"},
            ],
        },
        "wrapper2": {
            "list": {"title": "Double Wrapped Album", "count": 1, "level": 4},
            "items": [
                {"title": "Double Wrapped Album", "item_key": "7:2",
                 "hint": "list", "_goes_to": "contents"},
            ],
        },
        "contents": {
            "list": {"title": "Double Wrapped Album", "count": 2, "level": 5},
            "items": [
                {"title": "Play Album", "item_key": "7:3",
                 "hint": "action_list", "_goes_to": "play_album_actions"},
                {"title": "Track 1", "item_key": "7:4",
                 "hint": "action_list", "_goes_to": "track_actions"},
            ],
        },
        "play_album_actions": {
            "list": {"title": "Play Album", "count": 4, "level": 6,
                     "hint": "action_list"},
            "items": [
                {"title": "Play Now", "item_key": "7:20", "hint": "action"},
                {"title": "Add Next", "item_key": "7:21", "hint": "action"},
                {"title": "Queue", "item_key": "7:22", "hint": "action"},
                {"title": "Start Radio", "item_key": "7:23", "hint": "action"},
            ],
        },
    }


class TestActionDepthMargin(unittest.TestCase):
    def test_a_second_wrapper_level_still_resolves_within_the_raised_depth(self):
        # The real, measured album path (deep_wrapper_levels above) needs
        # exactly 3 loop passes -- range(3) already covers it, so that
        # fixture alone cannot tell MAX_ACTION_DEPTH=3 apart from 4. This
        # one needs 4 passes, genuinely exercising the raised limit.
        api = FakeRoon(double_wrapper_levels())
        s = browse.BrowseSession(api, "widget", ZoneHolder())
        reply = s.search("oingo boingo")
        api.calls.clear()
        out = s.play(0, "play_now", reply["level_id"])
        self.assertIs(out["played"], True)
        pop_calls = [c["pop_levels"] for c in api.calls if "pop_levels" in c]
        self.assertEqual(pop_calls, [5])


if __name__ == "__main__":
    unittest.main()
