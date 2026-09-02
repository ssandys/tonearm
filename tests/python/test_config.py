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

    # --- hardening: marketplace security review 2026-09-01 ---------------
    #
    # "Token/config reads are unbounded and symlink-following, while
    # predictable `.tmp` writes lack exclusive/no-follow creation."

    def test_load_refuses_a_symlinked_config(self):
        # A symlink planted at the config path must not be read through: the
        # daemon would otherwise parse, and later overwrite, whatever it aims
        # at. Refusal degrades to defaults, the same as an absent file.
        victim = os.path.join(self.tmp.name, "victim.json")
        with open(victim, "w") as handle:
            json.dump({"host": "attacker"}, handle)
        os.makedirs(os.path.dirname(config.CONFIG_PATH), exist_ok=True)
        os.symlink(victim, config.CONFIG_PATH)
        self.assertIsNone(config.load()["host"])

    def test_load_token_refuses_a_symlinked_token(self):
        # The pairing token is the most sensitive file tonearm owns. Reading
        # through a symlink would hand the daemon's read (and its later
        # 0600 write) to a path someone else chose.
        victim = os.path.join(self.tmp.name, "victim.token")
        with open(victim, "w") as handle:
            handle.write("stolen")
        os.makedirs(os.path.dirname(config.TOKEN_PATH), exist_ok=True)
        os.symlink(victim, config.TOKEN_PATH)
        self.assertIsNone(config.load_token())

    def test_load_refuses_an_oversized_config(self):
        # json.load() on a handle reads until EOF. A file larger than any
        # real config is not one, and must not be buffered into the daemon.
        os.makedirs(os.path.dirname(config.CONFIG_PATH), exist_ok=True)
        with open(config.CONFIG_PATH, "w") as handle:
            handle.write(" " * (config.MAX_CONFIG_BYTES + 1))
        self.assertIsNone(config.load()["host"])

    def test_load_token_refuses_an_oversized_token(self):
        os.makedirs(os.path.dirname(config.TOKEN_PATH), exist_ok=True)
        with open(config.TOKEN_PATH, "w") as handle:
            handle.write("x" * (config.MAX_TOKEN_BYTES + 1))
        self.assertIsNone(config.load_token())

    def test_save_does_not_write_through_a_planted_tmp_symlink(self):
        # The old temp name was `<path>.tmp` opened O_CREAT|O_TRUNC -- no
        # O_EXCL, no O_NOFOLLOW -- so anything able to plant a symlink at
        # that predictable name redirected the daemon's write to its target.
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as handle:
            handle.write("untouched")
        os.makedirs(os.path.dirname(config.CONFIG_PATH), mode=0o700, exist_ok=True)
        os.symlink(victim, config.CONFIG_PATH + ".tmp")
        config.save({"host": "h"})
        with open(victim) as handle:
            self.assertEqual(handle.read(), "untouched")

    def test_save_token_does_not_write_through_a_planted_tmp_symlink(self):
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as handle:
            handle.write("untouched")
        os.makedirs(os.path.dirname(config.TOKEN_PATH), mode=0o700, exist_ok=True)
        os.symlink(victim, config.TOKEN_PATH + ".tmp")
        config.save_token("secret")
        with open(victim) as handle:
            self.assertEqual(handle.read(), "untouched")

    def test_save_leaves_no_temp_file_behind(self):
        # An unpredictable temp name is only an improvement if it is still
        # cleaned up; otherwise the config dir accretes one file per write.
        config.save({"host": "h"})
        config.save_token("t")
        root = os.path.dirname(config.CONFIG_PATH)
        self.assertEqual(sorted(os.listdir(root)), ["config.json", "token"])

    def test_save_tightens_a_loose_config_directory(self):
        # makedirs(mode=0o700) applies only when it CREATES the directory.
        # A pre-existing world-readable ~/.config/tonearm would leave the
        # token readable by anyone, whatever mode the file itself carries.
        root = os.path.dirname(config.CONFIG_PATH)
        os.makedirs(root, exist_ok=True)
        os.chmod(root, 0o755)
        config.save_token("secret")
        self.assertEqual(os.stat(root).st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
