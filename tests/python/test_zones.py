import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import zones


def z(zid, name, st):
    return {"id": zid, "name": name, "state": st, "pinned": False}


class TestArbiter(unittest.TestCase):
    def test_nothing_playing_selects_nothing(self):
        a = zones.Arbiter()
        a.observe([z("1", "A", "stopped"), z("2", "B", "stopped")])
        self.assertIsNone(a.select([z("1", "A", "stopped"), z("2", "B", "stopped")]))

    def test_follows_the_only_playing_zone(self):
        a = zones.Arbiter()
        listing = [z("1", "A", "playing"), z("2", "B", "stopped")]
        a.observe(listing)
        self.assertEqual(a.select(listing)["id"], "1")

    def test_follows_the_most_recently_started_zone(self):
        a = zones.Arbiter()
        a.observe([z("1", "A", "playing"), z("2", "B", "stopped")])
        a.observe([z("1", "A", "playing"), z("2", "B", "playing")])
        listing = [z("1", "A", "playing"), z("2", "B", "playing")]
        self.assertEqual(a.select(listing)["id"], "2")

    def test_a_zone_already_playing_does_not_re_win_on_every_push(self):
        # "Most recently started" is the TRANSITION into playing, not presence
        # in the playing set -- otherwise a repeated identical push would keep
        # re-stamping zone 1 and the bar would flip back and forth.
        a = zones.Arbiter()
        a.observe([z("1", "A", "playing"), z("2", "B", "stopped")])
        a.observe([z("1", "A", "playing"), z("2", "B", "playing")])
        a.observe([z("1", "A", "playing"), z("2", "B", "playing")])
        listing = [z("1", "A", "playing"), z("2", "B", "playing")]
        self.assertEqual(a.select(listing)["id"], "2")

    def test_a_pin_overrides_a_playing_zone(self):
        a = zones.Arbiter()
        a.pin("1")
        listing = [z("1", "A", "stopped"), z("2", "B", "playing")]
        a.observe(listing)
        selected = a.select(listing)
        self.assertEqual(selected["id"], "1")
        self.assertTrue(selected["pinned"])

    def test_unpinning_restores_auto_follow(self):
        a = zones.Arbiter()
        a.pin("1")
        listing = [z("1", "A", "stopped"), z("2", "B", "playing")]
        a.observe(listing)
        a.unpin()
        self.assertEqual(a.select(listing)["id"], "2")

    def test_a_pin_to_a_vanished_zone_falls_back_rather_than_showing_nothing(self):
        a = zones.Arbiter()
        a.pin("gone")
        listing = [z("2", "B", "playing")]
        a.observe(listing)
        self.assertEqual(a.select(listing)["id"], "2")

    def test_the_selected_zone_reports_pinned_only_when_actually_pinned(self):
        a = zones.Arbiter()
        listing = [z("1", "A", "playing")]
        a.observe(listing)
        self.assertFalse(a.select(listing)["pinned"])

    def test_paused_still_counts_as_the_followed_zone(self):
        # Pausing must not make the widget jump to a different room.
        a = zones.Arbiter()
        a.observe([z("1", "A", "playing")])
        a.observe([z("1", "A", "paused")])
        listing = [z("1", "A", "paused"), z("2", "B", "stopped")]
        self.assertEqual(a.select(listing)["id"], "1")

    def test_select_does_not_mutate_the_caller_s_zone(self):
        a = zones.Arbiter()
        a.pin("1")
        listing = [z("1", "A", "playing")]
        a.observe(listing)
        a.select(listing)
        self.assertFalse(listing[0]["pinned"])

    def test_recency_wins_even_when_the_more_recent_zone_has_the_smaller_id(self):
        # Regression guard: a transition-blind implementation that just
        # picked max(active, key=id) would pass every other test in this
        # file, because in all of them the more-recently-started zone
        # also happens to have the larger id. Here zone "2" starts first
        # and zone "1" starts later, so recency and id order disagree --
        # only real recency tracking gets this right.
        a = zones.Arbiter()
        a.observe([z("2", "B", "playing"), z("1", "A", "stopped")])
        a.observe([z("2", "B", "playing"), z("1", "A", "playing")])
        listing = [z("2", "B", "playing"), z("1", "A", "playing")]
        self.assertEqual(a.select(listing)["id"], "1")

    def test_the_remaining_zone_takes_over_when_the_leader_pauses_and_holds_after_it_also_pauses(self):
        # Regression: B starts after A and becomes the followed zone. When
        # B pauses, A is still playing and must take over -- that is not a
        # transition into playing for anyone, so an implementation that
        # only updates the followed zone on such a transition would keep
        # pointing at B. Once A also pauses, the bar must stay on A (the
        # zone that was actually last playing), not snap back to B.
        a = zones.Arbiter()
        a.observe([z("1", "A", "playing")])

        a.observe([z("1", "A", "playing"), z("2", "B", "playing")])
        listing = [z("1", "A", "playing"), z("2", "B", "playing")]
        self.assertEqual(a.select(listing)["id"], "2")

        a.observe([z("1", "A", "playing"), z("2", "B", "paused")])
        listing = [z("1", "A", "playing"), z("2", "B", "paused")]
        self.assertEqual(a.select(listing)["id"], "1")

        a.observe([z("1", "A", "paused"), z("2", "B", "paused")])
        listing = [z("1", "A", "paused"), z("2", "B", "paused")]
        self.assertEqual(a.select(listing)["id"], "1")


if __name__ == "__main__":
    unittest.main()


class TestTheArbiterDoesNotGrowForever(unittest.TestCase):
    """Zone ids are wire data used as permanent dict keys.

    `observe()` recorded every id it ever saw in `_started_at` and
    `_last_state` and removed none, so both grew for the life of the
    process. This is not only an abuse case: Roon mints a NEW zone id when
    zones are grouped or ungrouped, so an ordinary household accumulates
    entries just by grouping rooms. 0.10.0 bounded every other collection
    keyed on wire data; this one was missed.
    """

    def test_ids_absent_from_the_listing_are_forgotten(self):
        arb = zones.Arbiter()
        for i in range(200):
            # Each push is a different ephemeral zone, as a regroup produces.
            arb.observe([{"id": "ephemeral-%d" % i, "state": "playing"}])
        arb.observe([{"id": "kitchen", "state": "playing"}])
        self.assertLessEqual(len(arb._started_at), 2)
        self.assertLessEqual(len(arb._last_state), 2)

    def test_the_zone_being_followed_survives_being_forgotten(self):
        # select() falls back to _last_followed when nothing is active, so
        # pruning must not evict the entry that fallback depends on.
        arb = zones.Arbiter()
        arb.observe([{"id": "kitchen", "state": "playing"}])
        arb.observe([{"id": "kitchen", "state": "paused"}])
        self.assertIn("kitchen", arb._last_state)
        picked = arb.select([{"id": "kitchen", "state": "paused"}])
        self.assertEqual(picked["id"], "kitchen")

    def test_recency_still_ranks_correctly_after_pruning(self):
        # The prune must not disturb the ordering it shares state with.
        arb = zones.Arbiter()
        arb.observe([{"id": "a", "state": "playing"},
                     {"id": "b", "state": "stopped"}])
        arb.observe([{"id": "a", "state": "playing"},
                     {"id": "b", "state": "playing"}])
        picked = arb.select([{"id": "a", "state": "playing"},
                             {"id": "b", "state": "playing"}])
        self.assertEqual(picked["id"], "b")   # b started more recently

    def test_a_zone_that_leaves_and_returns_playing_counts_as_newly_started(self):
        # Forgetting a departed zone means its return is a transition again,
        # which is what actually happened from the listener's point of view.
        arb = zones.Arbiter()
        arb.observe([{"id": "old", "state": "playing"}])
        arb.observe([{"id": "new", "state": "playing"}])          # old is gone
        arb.observe([{"id": "new", "state": "playing"},
                     {"id": "old", "state": "playing"}])          # old returns
        picked = arb.select([{"id": "new", "state": "playing"},
                             {"id": "old", "state": "playing"}])
        self.assertEqual(picked["id"], "old")


class TestGroupingChurnFromARealCore(unittest.TestCase):
    """The transition measured on a real Core on 2026-09-19.

    Captured by polling the daemon's own status every 2s while two Sonos
    speakers were grouped and then ungrouped. The group is a zone of its own
    and lives only as long as the grouping; the members keep their ids across
    the cycle. Real ids, so this pins behaviour against something Roon
    actually did rather than something modelled.
    """

    LIVING_ROOM_STEREO = "1601bdb56757fb6c57dedd8a2d4adcfcd486"
    CHIMAERA = "16012352e4acb1f5e9bae8bec7bf5df87fa4"
    GROUP = "160141644c41c0f46b48a526b5dbed57e530"      # "Sonos Move + 1"
    LIVING_ROOM = "16013f624837e553d24437fb74e3848da6f7"
    SONOS_MOVE = "160103a37af930504bd1135c69d08d4ea53b"

    def _grouped(self):
        return [{"id": self.LIVING_ROOM_STEREO, "state": "paused"},
                {"id": self.CHIMAERA, "state": "stopped"},
                {"id": self.GROUP, "state": "playing"}]

    def _ungrouped(self):
        return [{"id": self.LIVING_ROOM_STEREO, "state": "paused"},
                {"id": self.CHIMAERA, "state": "stopped"},
                {"id": self.LIVING_ROOM, "state": "stopped"},
                {"id": self.SONOS_MOVE, "state": "stopped"}]

    def test_the_dissolved_group_is_forgotten(self):
        arb = zones.Arbiter()
        arb.observe(self._grouped())
        arb.select(self._grouped())
        arb.observe(self._ungrouped())
        self.assertNotIn(self.GROUP, arb._started_at)

    def test_repeated_grouping_does_not_accumulate(self):
        arb = zones.Arbiter()
        for _ in range(50):
            arb.observe(self._grouped())
            arb.select(self._grouped())
            arb.observe(self._ungrouped())
            arb.select(self._ungrouped())
        # Exactly the live listing: four zones, no residue from 50 groupings.
        self.assertLessEqual(len(arb._started_at), 4)
        self.assertLessEqual(len(arb._last_state), 4)

    def test_the_group_can_still_be_followed_while_it_exists(self):
        # The group is the zone actually playing, so it must be selectable --
        # forgetting must not make a live group unfollowable.
        arb = zones.Arbiter()
        arb.observe(self._grouped())
        self.assertEqual(arb.select(self._grouped())["id"], self.GROUP)
