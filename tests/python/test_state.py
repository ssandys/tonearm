import json
import os
import sys
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
        vol = state.normalize_zone(self.raw)["volume"]
        self.assertEqual(vol["value"], 62)
        self.assertEqual(vol["max"], 100)
        self.assertFalse(vol["muted"])

    def test_a_zone_with_no_outputs_has_no_volume(self):
        raw = dict(self.raw, outputs=[])
        self.assertIsNone(state.normalize_zone(raw)["volume"])

    def test_a_fixed_volume_output_reports_none(self):
        # Many streamers expose type "incremental" or no volume object at all;
        # rendering a slider for those would be a lie.
        raw = json.loads(json.dumps(self.raw))
        del raw["outputs"][0]["volume"]
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
