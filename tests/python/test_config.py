import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        config.reset_paths()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev
        config.reset_paths()

    def test_load_returns_defaults_when_absent(self):
        cfg = config.load()
        self.assertIsNone(cfg["host"])
        self.assertEqual(cfg["tcp_port"], 9150)
        self.assertEqual(cfg["http_port"], 9330)
        self.assertIsNone(cfg["pinned_zone_id"])

    def test_save_then_load_round_trips(self):
        config.save({"host": "192.168.50.118", "tcp_port": 9150,
                     "http_port": 9330, "name": "yavin", "pinned_zone_id": "z1"})
        cfg = config.load()
        self.assertEqual(cfg["host"], "192.168.50.118")
        self.assertEqual(cfg["pinned_zone_id"], "z1")

    def test_load_survives_a_corrupt_file(self):
        # A truncated write must degrade to defaults, not crash the daemon on
        # boot -- systemd would restart it into the same crash forever.
        os.makedirs(os.path.dirname(config.CONFIG_PATH), exist_ok=True)
        with open(config.CONFIG_PATH, "w") as handle:
            handle.write("{not json")
        self.assertIsNone(config.load()["host"])

    def test_save_is_atomic(self):
        # Written via a temp file and renamed, so a crash mid-write cannot
        # leave a half-file that the corrupt-file path then has to absorb.
        config.save({"host": "h"})
        self.assertFalse(os.path.exists(config.CONFIG_PATH + ".tmp"))
        with open(config.CONFIG_PATH) as handle:
            self.assertEqual(json.load(handle)["host"], "h")

    def test_token_helpers_round_trip_and_tolerate_absence(self):
        self.assertIsNone(config.load_token())
        config.save_token("abc123")
        self.assertEqual(config.load_token(), "abc123")

    def test_config_is_not_world_readable(self):
        config.save({"host": "h"})
        self.assertEqual(os.stat(config.CONFIG_PATH).st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
