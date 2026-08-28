import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import mpris

CORE = {"host": "192.168.50.118", "http_port": 9330, "name": "yavin"}
PLAYING = {
    "v": 1, "status": "ok", "core": CORE,
    "zone": {"id": "z1", "name": "Living Room", "state": "playing", "pinned": False,
             "volume": {"value": 62, "min": 0, "max": 100, "step": 1, "muted": False},
             "position": 271, "length": 585,
             "now_playing": {"title": "Blue Train", "artist": "John Coltrane",
                             "album": "Blue Train", "image_key": "a1b2"}},
    "zones": [],
}


class TestMetadata(unittest.TestCase):
    def test_maps_title_artist_album(self):
        md = mpris.metadata_for(PLAYING)
        self.assertEqual(md["xesam:title"], "Blue Train")
        self.assertEqual(md["xesam:artist"], ["John Coltrane"])
        self.assertEqual(md["xesam:album"], "Blue Train")

    def test_artist_is_a_list_even_for_one_artist(self):
        # xesam:artist is `as` in the spec; a bare string makes strict clients
        # drop the whole metadata dict.
        self.assertIsInstance(mpris.metadata_for(PLAYING)["xesam:artist"], list)

    def test_length_is_microseconds(self):
        self.assertEqual(mpris.metadata_for(PLAYING)["mpris:length"], 585_000_000)

    def test_art_url_points_at_the_core_s_http_port(self):
        self.assertEqual(mpris.metadata_for(PLAYING)["mpris:artUrl"],
                         "http://192.168.50.118:9330/api/image/a1b2"
                         "?scale=fit&width=512&height=512")

    def test_track_id_is_a_valid_object_path(self):
        # A bad path makes some clients disconnect from the bus entirely.
        track_id = mpris.metadata_for(PLAYING)["mpris:trackid"]
        self.assertTrue(track_id.startswith("/com/onemanposse/tonearm/"))
        self.assertNotIn("-", track_id.rsplit("/", 1)[-1])

    def test_no_track_yields_empty_metadata_not_a_crash(self):
        idle = {"v": 1, "status": "ok", "core": CORE, "zone": None, "zones": []}
        self.assertEqual(mpris.metadata_for(idle), {})

    def test_art_url_is_omitted_when_there_is_no_image_key(self):
        import copy
        payload = copy.deepcopy(PLAYING)
        payload["zone"]["now_playing"]["image_key"] = ""
        self.assertNotIn("mpris:artUrl", mpris.metadata_for(payload))


class TestPlaybackStatus(unittest.TestCase):
    def test_maps_roon_states_to_mpris_names(self):
        self.assertEqual(mpris.playback_status("playing"), "Playing")
        self.assertEqual(mpris.playback_status("paused"), "Paused")
        self.assertEqual(mpris.playback_status("stopped"), "Stopped")
        self.assertEqual(mpris.playback_status("loading"), "Playing")

    def test_an_unknown_state_is_stopped_rather_than_an_exception(self):
        self.assertEqual(mpris.playback_status("constructor"), "Stopped")
        self.assertEqual(mpris.playback_status(None), "Stopped")


if __name__ == "__main__":
    unittest.main()
