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


if __name__ == "__main__":
    unittest.main()
