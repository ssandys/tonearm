import json
import os
import sys
import json
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import state

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return json.load(handle)


class TestNormalizeZone(unittest.TestCase):
    def setUp(self):
        self.raw = fixture("zone_playing.json")

    def test_maps_the_fields_the_widget_renders(self):
        z = state.normalize_zone(self.raw)
        self.assertEqual(z["id"], "1601abc")
        self.assertEqual(z["name"], "Living Room")
        self.assertEqual(z["state"], "playing")
        self.assertEqual(z["position"], 271)
        self.assertEqual(z["length"], 585)

    def test_three_line_becomes_title_artist_album(self):
        np = state.normalize_zone(self.raw)["now_playing"]
        self.assertEqual(np["title"], "Blue Train")
        self.assertEqual(np["artist"], "John Coltrane")
        self.assertEqual(np["album"], "Blue Train")
        self.assertEqual(np["image_key"], "a1b2c3")

    def test_volume_comes_from_the_first_output(self):
        # The fixture's second output ("o2") deliberately has a different
        # value, min, max and muted state -- min=-80, max=0, value=-20,
        # muted=True -- so if _volume_of ever read the wrong output (e.g.
        # outputs[-1] instead of outputs[0]) every one of these assertions
        # would fail. max=70 is also deliberately not `max`'s implementation
        # default of 100, so a stub that only returns defaults cannot pass
        # this either.
        vol = state.normalize_zone(self.raw)["volume"]
        self.assertEqual(vol["value"], 62)
        self.assertEqual(vol["max"], 70)
        self.assertFalse(vol["muted"])

    def test_a_zone_with_no_outputs_has_no_volume(self):
        raw = dict(self.raw, outputs=[])
        self.assertIsNone(state.normalize_zone(raw)["volume"])

    def test_a_fixed_volume_output_reports_none(self):
        # A fixed-volume output has no volume object at all; rendering a
        # slider for it would be a lie. See
        # test_an_incremental_volume_output_reports_none for the other
        # shape a volume-less output takes.
        raw = json.loads(json.dumps(self.raw))
        del raw["outputs"][0]["volume"]
        self.assertIsNone(state.normalize_zone(raw)["volume"])

    def test_an_incremental_volume_output_reports_none(self):
        # Many streamers expose type "incremental": relative up/down steps
        # only, with no absolute value/min/max to read or set. Previously
        # only the "no volume object at all" case (above) was excluded, so
        # this fell through to a fabricated {"value": 0, "min": 0, "max":
        # 100} -- a slider parked at zero for a zone whose volume this can
        # neither read nor set.
        raw = json.loads(json.dumps(self.raw))
        raw["outputs"][0]["volume"] = {"type": "incremental", "is_muted": False}
        self.assertIsNone(state.normalize_zone(raw)["volume"])

    def test_a_stopped_zone_has_no_now_playing(self):
        raw = json.loads(json.dumps(self.raw))
        raw["state"] = "stopped"
        del raw["now_playing"]
        z = state.normalize_zone(raw)
        self.assertEqual(z["state"], "stopped")
        self.assertIsNone(z["now_playing"])

    def test_missing_three_line_does_not_raise(self):
        raw = json.loads(json.dumps(self.raw))
        del raw["now_playing"]["three_line"]
        np = state.normalize_zone(raw)["now_playing"]
        self.assertEqual(np["title"], "")
        self.assertEqual(np["image_key"], "a1b2c3")

    def test_now_playing_carries_a_nullable_art_path_placeholder(self):
        # state.py does no I/O (see its module docstring), so it cannot know
        # whether a local cached copy exists. It only ever declares the slot;
        # the daemon's art cache (art.py) fills it in, or leaves it null.
        np = state.normalize_zone(self.raw)["now_playing"]
        self.assertIn("art_path", np)
        self.assertIsNone(np["art_path"])

    def test_none_in_none_out(self):
        self.assertIsNone(state.normalize_zone(None))


class TestBuild(unittest.TestCase):
    def test_emits_the_v1_envelope(self):
        core = {"host": "192.168.50.118", "http_port": 9330, "name": "yavin"}
        payload = state.build("ok", core, None, [])
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["core"],
                         {"host": "192.168.50.118", "http_port": 9330, "name": "yavin"})
        self.assertIsNone(payload["zone"])
        self.assertEqual(payload["zones"], [])

    def test_core_is_trimmed_to_the_three_fields_the_widget_uses(self):
        core = {"host": "h", "http_port": 1, "name": "n",
                "tcp_port": 9150, "unique_id": "x", "via": "scan"}
        self.assertEqual(set(state.build("ok", core, None, [])["core"]),
                         {"host", "http_port", "name"})

    def test_zones_carry_only_id_name_and_state(self):
        zones = [{"id": "z1", "name": "Living Room", "state": "playing",
                  "volume": {"value": 62}, "now_playing": {"title": "x"}}]
        out = state.build("ok", {"host": "h", "http_port": 1, "name": "n"}, None, zones)
        self.assertEqual(out["zones"], [{"id": "z1", "name": "Living Room", "state": "playing"}])

    def test_status_must_be_one_of_the_four(self):
        with self.assertRaises(ValueError):
            state.build("bogus", None, None, [])


if __name__ == "__main__":
    unittest.main()


class TestSeekPosition(unittest.TestCase):
    """Where `seek_position` actually lives after a zones_seek_changed event.

    Roon pushes a seek update roughly once a second carrying only
    {zone_id, seek_position, queue_time_remaining}. roonapi merges that with
    `self._zones[zone_id].update(zone)` (roonapi.py:900), which lands
    `seek_position` at the TOP LEVEL of the zone dict -- it never reaches the
    nested `now_playing`, which is refreshed only by a full `zones_changed`.

    Reading only `now_playing.seek_position` therefore froze the reported
    position at whatever it was during the last full zone update. Measured
    live against the Core: 13 pushes in 12 seconds, every one `position: 0`,
    while `length` changed 150 -> 320 as the track advanced. The widget's own
    clock-side extrapolation could not paper over it either, because each of
    those pushes resets `receivedAt`, so the elapsed term never exceeds ~1s.
    """

    def _zone(self, top=None, nested=None):
        z = {
            "zone_id": "z1",
            "display_name": "Living Room",
            "state": "playing",
            "now_playing": {
                "length": 320,
                "three_line": {"line1": "t", "line2": "a", "line3": "b"},
            },
        }
        if top is not None:
            z["seek_position"] = top
        if nested is not None:
            z["now_playing"]["seek_position"] = nested
        return z

    def test_reads_the_top_level_seek_position(self):
        # The one a zones_seek_changed event actually updates.
        self.assertEqual(state.normalize_zone(self._zone(top=137))["position"], 137)

    def test_top_level_wins_over_a_stale_nested_value(self):
        # now_playing is only as fresh as the last full zones_changed, so the
        # top-level value is the newer of the two whenever both are present.
        z = self._zone(top=137, nested=0)
        self.assertEqual(state.normalize_zone(z)["position"], 137)

    def test_falls_back_to_the_nested_value(self):
        # A full zones_changed arriving before any seek event has no top-level
        # key at all; that payload's nested position is the only one there is.
        self.assertEqual(state.normalize_zone(self._zone(nested=42))["position"], 42)

    def test_a_genuine_zero_is_not_mistaken_for_missing(self):
        # Track start reports 0, which is a real position. If `or` chose the
        # fallback on a falsy 0, a seek back to the start would show the
        # previous track's stale offset instead.
        z = self._zone(top=0, nested=271)
        self.assertEqual(state.normalize_zone(z)["position"], 0)

    def test_neither_present_is_zero_not_none(self):
        # The widget divides by length and formats this; None would render
        # "NaN" and poison the seek fill's width binding.
        self.assertEqual(state.normalize_zone(self._zone())["position"], 0)


class TestCoreSuppliedTextIsBounded(unittest.TestCase):
    """Strings from the Core reach a process shared with every other widget.

    Every bound in the daemon constrained the socket client -- request line,
    session key, search term, zone id -- or art and config on disk. Nothing
    constrained the Core, whose zone names and track metadata are rendered by
    Panel.qml inside omarchy-shell and published onto the session bus by
    mpris.py. CONTRIBUTING.md states the rule ("anything unbounded it
    consumes ... can stall or exhaust the whole shell") and it was applied in
    one direction only.

    Clipped rather than refused: these are display strings, and showing a
    truncated title is better than showing none. That is the opposite of the
    MAX_SOOD_FIELD decision, deliberately -- an oversized SOOD field is
    dropped because it feeds identity matching, not a label.
    """

    def _zone(self, **over):
        base = {"zone_id": "z1", "display_name": "Kitchen", "state": "playing"}
        base.update(over)
        return base

    def test_a_zone_name_is_clipped(self):
        z = state.normalize_zone(self._zone(display_name="n" * 5000))
        self.assertEqual(len(z["name"]), state.MAX_TEXT)

    def test_track_metadata_is_clipped(self):
        z = state.normalize_zone(self._zone(now_playing={
            "three_line": {"line1": "t" * 5000,
                           "line2": "a" * 5000,
                           "line3": "b" * 5000}}))
        np = z["now_playing"]
        for field in ("title", "artist", "album"):
            self.assertEqual(len(np[field]), state.MAX_TEXT, field)

    def test_an_ordinary_title_is_untouched(self):
        z = state.normalize_zone(self._zone(now_playing={
            "three_line": {"line1": "Speak to Me"}}))
        self.assertEqual(z["now_playing"]["title"], "Speak to Me")

    def test_the_core_name_is_clipped(self):
        built = state.build("ok", {"host": "h", "name": "y" * 5000}, None, [])
        self.assertEqual(len(built["core"]["name"]), state.MAX_TEXT)

    def test_the_zone_list_is_capped(self):
        many = [{"id": "z%d" % i, "name": "n", "state": "stopped"}
                for i in range(state.MAX_ZONES + 50)]
        built = state.build("ok", None, None, many)
        self.assertEqual(len(built["zones"]), state.MAX_ZONES)

    def test_names_in_the_zone_list_are_clipped_too(self):
        built = state.build("ok", None, None,
                            [{"id": "z1", "name": "n" * 5000, "state": "stopped"}])
        self.assertEqual(len(built["zones"][0]["name"]), state.MAX_TEXT)


class TestEveryCoreSuppliedValueIsBounded(unittest.TestCase):
    """The marketplace review blocked publication on this (2026-09-30).

    `MAX_TEXT` bounds display labels and `MAX_ZONES` bounds the zone COUNT,
    but `zone_id`, `state` and `core.host` were forwarded exactly as the Core
    sent them. The payload is then published to every subscriber, and the
    widget's relay parses and retains the whole line inside omarchy-shell --
    the process every bar widget shares. So one oversized zone field from the
    connected Core could exhaust shell memory while every existing bound
    looked satisfied.

    The reviewer named `zone_id` and `state`; `core.host`, `image_key` and the
    numerics are the same fault in the same function and are fixed with them
    rather than waiting to be found next.
    """

    def test_an_oversized_zone_id_cannot_reach_the_payload(self):
        huge = "z" * 100_000
        z = state.normalize_zone({"zone_id": huge, "display_name": "Kitchen",
                                  "state": "playing"})
        self.assertLessEqual(len(z["id"]), state.MAX_ID)

    def test_an_oversized_state_cannot_reach_the_payload(self):
        z = state.normalize_zone({"zone_id": "z1", "display_name": "Kitchen",
                                  "state": "p" * 100_000})
        self.assertLessEqual(len(z["state"]), state.MAX_STATE)

    def test_an_oversized_core_host_cannot_reach_the_payload(self):
        built = state.build("ok", {"host": "h" * 100_000, "name": "yavin"},
                            None, [])
        self.assertLessEqual(len(built["core"]["host"]), state.MAX_ID)

    def test_an_oversized_image_key_cannot_reach_the_payload(self):
        z = state.normalize_zone({
            "zone_id": "z1", "display_name": "K", "state": "playing",
            "now_playing": {"three_line": {"line1": "t"},
                            "image_key": "k" * 100_000}})
        self.assertLessEqual(len(z["now_playing"]["image_key"]), state.MAX_ID)

    def test_a_non_string_image_key_does_not_propagate(self):
        # #28: a non-string image_key reached art.Cache.get and raised
        # TypeError inside snapshot(), which is the single source for every
        # subscribe, status reply and broadcast -- so the widget received
        # nothing at all while that zone was followed.
        z = state.normalize_zone({
            "zone_id": "z1", "display_name": "K", "state": "playing",
            "now_playing": {"three_line": {"line1": "t"}, "image_key": {"a": 1}}})
        self.assertIsInstance(z["now_playing"]["image_key"], str)

    def test_a_string_position_is_not_carried_as_a_string(self):
        # #21: position and length are multiplied by 1_000_000 by the MPRIS
        # adapter. For a number that is a unit conversion; for a STRING it is
        # Python's sequence repetition, so a 4 KB string becomes a ~4 GB
        # string, fully allocated, before int() rejects it.
        z = state.normalize_zone({
            "zone_id": "z1", "display_name": "K", "state": "playing",
            "seek_position": "9" * 4096,
            "now_playing": {"three_line": {"line1": "t"}, "length": "8" * 4096}})
        self.assertIsInstance(z["position"], (int, float))
        self.assertIsInstance(z["length"], (int, float))

    def test_non_numeric_volume_fields_are_coerced(self):
        z = state.normalize_zone({
            "zone_id": "z1", "display_name": "K", "state": "playing",
            "outputs": [{"volume": {"value": "loud", "min": None,
                                    "max": ["x"], "step": {}}}]})
        for field in ("value", "min", "max", "step"):
            self.assertIsInstance(z["volume"][field], (int, float), field)

    def test_the_whole_payload_is_bounded_even_at_max_zones(self):
        # The property the review is really about: every field bounded, times
        # the zone cap, must still be a sane line to hand a shell.
        hostile = [{"id": "z" * 100_000, "name": "n" * 100_000,
                    "state": "s" * 100_000} for _ in range(500)]
        built = state.build("ok", {"host": "h" * 100_000, "name": "c" * 100_000},
                            None, hostile)
        size = len(json.dumps(built))
        self.assertLess(size, 512 * 1024, "payload was %d bytes" % size)


class TestQueueRemaining(unittest.TestCase):
    def test_optional_whole_queue_seconds_follow_updated_zone(self):
        raw = fixture('zone_playing.json')
        for value in (3572, 3571, 4000, 0, 3723.5):
            raw['queue_time_remaining'] = value
            self.assertEqual(state.normalize_zone(raw)['queue_time_remaining'], value)
        raw['state'] = 'paused'
        self.assertEqual(state.normalize_zone(raw)['queue_time_remaining'], 3723.5)

    def test_unknown_or_invalid_is_not_an_empty_queue(self):
        raw = fixture('zone_playing.json')
        raw.pop('queue_time_remaining', None)
        self.assertIsNone(state.normalize_zone(raw)['queue_time_remaining'])
        for value in (None, True, False, '3572', -1, float('nan'),
                      float('inf'), float('-inf'), state.MAX_NUMBER + 1, {}, []):
            with self.subTest(value=value):
                raw['queue_time_remaining'] = value
                self.assertIsNone(state.normalize_zone(raw)['queue_time_remaining'])
