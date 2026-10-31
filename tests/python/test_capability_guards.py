"""Backend seek and absolute-volume capability guards.

The transport layer refuses a `seek` whenever the selected zone's raw
`is_seek_allowed` flag is not exactly `True` (false, missing, or a non-bool
all fail). It refuses an absolute `volume` whenever `outputs[0]` is missing
its `output_id`, has a fixed/incremental `type`, or carries volume metadata
that isn't a dict of finite numeric (not bool) value/min/max with min<max
and the current value inside [min, max]. The write must always target
`outputs[0]` -- never a sibling in a grouped zone -- and the requested
value must fall in the same advertised bounds: refuse, never clamp. An
unknown or absent `type` with otherwise usable metadata is still supported
to keep Roon's forward compatibility. Refusals are logged and dropped like
every other command: no new wire fields, no structured results. Readiness,
selection, and local pin/unpin guards remain in force; mute and other
transport verbs are untouched.

Everything is synthetic: a tiny RecordingApi plus config.load/save patches.
No networking, no fakes, no threads.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))
from tonearm_lib import core


def _make_output(output_id="output-1",
                 volume=None,
                 overrides=None):
    """One zone output carrying optional `volume` metadata."""
    out = {"output_id": output_id}
    if volume is not None:
        out["volume"] = volume
    if overrides:
        out.update(overrides)
    return out


def _make_zone(zone_id, outputs, state="playing", **extra):
    z = {"zone_id": zone_id, "display_name": zone_id,
         "state": state, "outputs": outputs}
    z.update(extra)
    return z


def _number_volume(value, lo, hi):
    return {"type": "number", "value": value, "min": lo, "max": hi}


class RecordingApi:
    """The transport surface the capability guards can reach."""

    def __init__(self, zone, generation=1):
        # Generation at 1 satisfies `_zones_ready` against the default
        # `_required_zone_generation` (1) so the guards -- not readiness --
        # are what the test is exercising.
        self._zone_list_generation = generation
        self.zones = {zone["zone_id"]: zone}
        self.calls = []

    def playback_control(self, zid, verb):
        self.calls.append(("playback", zid, verb))

    def seek(self, zid, pos, mode):
        self.calls.append(("seek", zid, pos, mode))

    def change_volume_raw(self, output, value, mode):
        self.calls.append(("volume", output, value, mode))

    def mute(self, output, flag):
        self.calls.append(("mute", output, flag))

    def transfer_zone(self, source, dest):
        self.calls.append(("transfer", source, dest))


class TestCapabilityGuards(unittest.TestCase):
    def session(self, api, pin="zone-a"):
        """Build a session around `api`; config is patched for the whole test."""
        self.saved = []
        for patcher in (
                patch.object(core.config, "load",
                             return_value={"pinned_zone_id": pin,
                                           "strict_pins": True}),
                patch.object(core.config, "save",
                             lambda cfg: self.saved.append(dict(cfg)))):
            patcher.start()
            self.addCleanup(patcher.stop)
        s = core.RoonSession(lambda _: None)
        s._api, s._status = api, "ok"
        return s

    # --- seek guards ------------------------------------------------------

    def test_seek_allowed_when_is_seek_allowed_is_true(self):
        zone = _make_zone("zone-a", [_make_output(volume=_number_volume(30, 0, 100))],
                          is_seek_allowed=True)
        api = RecordingApi(zone)
        s = self.session(api)
        s.command("seek", 42)
        self.assertEqual(api.calls, [("seek", "zone-a", 42, "absolute")])

    def test_seek_refused_when_is_seek_allowed_is_false_or_missing(self):
        # False, missing, and non-bool all fail -- the guard is exact.
        for flag in (False, None, "true", 1, 0):
            with self.subTest(flag=flag):
                zone = _make_zone("zone-a",
                                  [_make_output(volume=_number_volume(30, 0, 100))])
                if flag is not None:
                    zone["is_seek_allowed"] = flag
                api = RecordingApi(zone)
                s = self.session(api)
                s.command("seek", 42)
                self.assertEqual(api.calls, [],
                                 "seek should be refused when is_seek_allowed=%r"
                                 % (flag,))

    def test_seek_does_not_require_volume_metadata_change(self):
        # Seek is a transport capability on the zone, not a property of
        # volume metadata -- its guard is independent of the volume check.
        zone = _make_zone("zone-a",
                          [_make_output(volume={"type": "fixed"})],
                          is_seek_allowed=True)
        api = RecordingApi(zone)
        s = self.session(api)
        s.command("seek", 7)
        self.assertEqual(api.calls, [("seek", "zone-a", 7, "absolute")])

    # --- volume guards ----------------------------------------------------

    def test_volume_allowed_for_first_output_only(self):
        # Grouped zone: outputs[0]'s scale is the one the widget reads.
        zone = _make_zone("zone-a", [
            _make_output("output-1", volume=_number_volume(30, 0, 100)),
            _make_output("output-2", volume=_number_volume(-40, -80, 0)),
        ])
        api = RecordingApi(zone)
        s = self.session(api)
        s.command("volume", 50)
        self.assertEqual(api.calls,
                         [("volume", "output-1", 50, "absolute")])
        # A value valid only for the sibling must be refused, not redirected.
        api.calls.clear()
        s.command("volume", -20)
        self.assertEqual(api.calls, [])

    def test_volume_refused_when_type_is_fixed_or_incremental(self):
        for bad_type in ("fixed", "incremental"):
            with self.subTest(type=bad_type):
                zone = _make_zone("zone-a", [
                    _make_output("output-1",
                                 volume={"type": bad_type, "value": 5,
                                         "min": 0, "max": 100})])
                api = RecordingApi(zone)
                s = self.session(api)
                s.command("volume", 50)
                self.assertEqual(api.calls, [])

    def test_volume_unknown_or_missing_type_with_usable_metadata_still_supported(self):
        # Forward compatibility: a future Roon `type` (or absent one) with
        # otherwise well-formed numeric metadata must still write.
        for vol in ({"value": 30, "min": 0, "max": 100},
                    {"type": "future_thing", "value": 30, "min": 0, "max": 100}):
            with self.subTest(volume=vol):
                zone = _make_zone("zone-a",
                                  [_make_output("output-1", volume=vol)])
                api = RecordingApi(zone)
                s = self.session(api)
                s.command("volume", 50)
                self.assertEqual(api.calls,
                                 [("volume", "output-1", 50, "absolute")])

    def test_volume_refused_when_metadata_is_unusable(self):
        # Every reason the metadata is unusable produces a refusal and no
        # call: missing/empty output_id, non-dict volume, non-numeric or
        # bool value/min/max, min>=max, current value out of bounds.
        bad_zones = [
            _make_zone("zone-a", [_make_output("", volume=_number_volume(30, 0, 100))]),
            _make_zone("zone-a", [_make_output("output-1", volume=None)]),
            _make_zone("zone-a", [_make_output("output-1", volume="loud")]),
            _make_zone("zone-a", [_make_output("output-1",
                                               volume=_number_volume(True, 0, 100))]),
            _make_zone("zone-a", [_make_output("output-1",
                                               volume={"type": "number",
                                                       "value": 30,
                                                       "min": True,
                                                       "max": 100})]),
            _make_zone("zone-a", [_make_output("output-1",
                                               volume={"type": "number",
                                                       "value": 30,
                                                       "min": 50,
                                                       "max": 50})]),
            _make_zone("zone-a", [_make_output("output-1",
                                               volume={"type": "number",
                                                       "value": 200,
                                                       "min": 0,
                                                       "max": 100})]),
        ]
        for key in ("value", "min", "max"):
            for invalid in (None, True, "30", float("nan"), float("inf")):
                vol = _number_volume(30, 0, 100)
                vol[key] = invalid
                bad_zones.append(_make_zone("zone-a", [_make_output(volume=vol)]))
            vol = _number_volume(30, 0, 100)
            del vol[key]
            bad_zones.append(_make_zone("zone-a", [_make_output(volume=vol)]))
        for zone in bad_zones:
            with self.subTest(zone_id=zone["zone_id"],
                              output_id=zone["outputs"][0].get("output_id"),
                              volume=zone["outputs"][0].get("volume")):
                api = RecordingApi(zone)
                s = self.session(api)
                s.command("volume", 50)
                self.assertEqual(api.calls, [])

    def test_volume_refused_when_request_outside_advertised_bounds(self):
        # Refuse, never clamp -- Roon accepts whatever we send.
        zone = _make_zone("zone-a",
                          [_make_output("output-1",
                                       volume=_number_volume(30, 0, 100))])
        api = RecordingApi(zone)
        s = self.session(api)
        for bad in (-1, 101):
            with self.subTest(arg=bad):
                s.command("volume", bad)
        self.assertEqual(api.calls, [],
                         "out-of-bounds volumes must be refused, not clamped")

    def test_volume_targets_only_first_output_after_fresh_listing(self):
        # A refresh may swap outputs[0]; the guard must still write to
        # whatever the current first output is, never a sibling.
        zone = _make_zone("zone-a", [
            _make_output("output-1", volume=_number_volume(30, 0, 100)),
            _make_output("output-2", volume=_number_volume(-40, -80, 0)),
        ])
        api = RecordingApi(zone)
        s = self.session(api)
        s.command("volume", 10)
        self.assertEqual(api.calls,
                         [("volume", "output-1", 10, "absolute")])
        api.calls.clear()
        # Replace outputs[0] with one that carries a different scale.
        api.zones["zone-a"]["outputs"][0] = _make_output(
            "output-3", volume=_number_volume(-20, -80, 0))
        s.command("volume", -10)
        self.assertEqual(api.calls,
                         [("volume", "output-3", -10, "absolute")])
        # And never the survivor of the previous first output.
        self.assertNotIn(("volume", "output-1", -10, "absolute"),
                         api.calls)

    def test_db_range_and_integer_coercion_remain_supported(self):
        vol = {"type": "db", "value": -50.5, "min": -80.5, "max": 10.5}
        api = RecordingApi(_make_zone("zone-a", [_make_output(volume=vol)]))
        s = self.session(api)
        for arg, expected in ((-40, -40), ("10", 10), (10.0, 10)):
            s.command("volume", arg)
            self.assertEqual(api.calls[-1], ("volume", "output-1", expected, "absolute"))

    def test_capability_changes_take_effect_without_replaying_commands(self):
        zone = _make_zone("zone-a", [_make_output(volume=_number_volume(30, 0, 100))],
                          is_seek_allowed=True)
        api = RecordingApi(zone)
        s = self.session(api)
        s.command("seek", 5)
        s.command("volume", 20)
        self.assertEqual(len(api.calls), 2)
        api.calls.clear()
        zone["is_seek_allowed"] = False
        zone["outputs"][0]["volume"] = {"type": "incremental"}
        s.command("seek", 5)
        s.command("volume", 20)
        self.assertEqual(api.calls, [])
        zone["is_seek_allowed"] = True
        zone["outputs"][0]["volume"] = _number_volume(30, 0, 100)
        s.snapshot()
        self.assertEqual(api.calls, [])

    def test_unusable_output_shapes_and_ids_are_refused(self):
        for outputs in ([], None, {}, {"0": {}}, "output", [None], ["output"],
                        [{"volume": _number_volume(30, 0, 100)}],
                        [_make_output(None, _number_volume(30, 0, 100))],
                        [_make_output(42, _number_volume(30, 0, 100))]):
            with self.subTest(outputs=outputs):
                api = RecordingApi(_make_zone("zone-a", outputs))
                s = self.session(api)
                with self.assertLogs(core.LOG, level="WARNING"):
                    s.command("volume", 20)
                self.assertEqual(api.calls, [])

    def test_capability_refusal_happens_before_argument_conversion(self):
        api = RecordingApi(_make_zone("zone-a", [_make_output()], is_seek_allowed=False))
        s = self.session(api)
        for verb in ("seek", "volume"):
            with self.assertLogs(core.LOG, level="WARNING"):
                s.command(verb, "not-a-number")
        self.assertEqual(api.calls, [])

    # --- combined gates ---------------------------------------------------

    def test_readiness_still_gates_capable_targets(self):
        # Even when metadata would otherwise permit the write, a stale
        # listing (generation < required) must drop the verb.
        zone = _make_zone("zone-a",
                          [_make_output(volume=_number_volume(30, 0, 100))],
                          is_seek_allowed=True)
        api = RecordingApi(zone, generation=1)
        s = self.session(api)
        s._required_zone_generation = 2
        s.command("seek", 42)
        s.command("volume", 50)
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()