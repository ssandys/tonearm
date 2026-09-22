"""`setup.sh` installs a systemd unit; it must not write through a plant.

`cp "$source_unit" "$target_unit"` followed a symlink at the destination, so a
link planted at ~/.config/systemd/user/tonearmd.service redirected the write to
whatever it named. Same class as both findings in the marketplace review of a
sibling plugin (HANCORE-linux/omarchy-plugin-marketplace#2659), and
docs/FOLLOWUPS.md item 4 already recorded that the script this was modelled on
guards against replacing an unrelated service file.

These run setup.sh for real, with a `systemctl` shim first on PATH so nothing
touches the live user session.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

SYSTEMCTL_SHIM = "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\nexit 0\n"


def _deps_present():
    """setup.sh probes /usr/bin/python for the daemon's two imports and exits
    early without them. A CI runner installs those into a different
    interpreter, so skip rather than assert an unrelated failure."""
    if not os.path.exists("/usr/bin/python"):
        return False
    for module in ("dbus_next", "websocket"):
        if subprocess.run(["/usr/bin/python", "-c", "import " + module],
                          capture_output=True).returncode != 0:
            return False
    return True


@unittest.skipUnless(_deps_present(), "daemon deps absent from /usr/bin/python")
class SetupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

        # setup.sh refuses to run from anywhere but the installed location.
        self.plugin = os.path.join(
            self.home, ".config", "omarchy", "plugins", "ssandys.tonearm")
        os.makedirs(self.plugin)
        shutil.copy(os.path.join(REPO, "setup.sh"), self.plugin)
        shutil.copytree(os.path.join(REPO, "systemd"),
                        os.path.join(self.plugin, "systemd"))
        os.makedirs(os.path.join(self.plugin, "scripts"))
        shutil.copytree(os.path.join(REPO, "scripts", "tonearm_lib"),
                        os.path.join(self.plugin, "scripts", "tonearm_lib"))

        self.unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        self.target = os.path.join(self.unit_dir, "tonearmd.service")

        shim_dir = os.path.join(self.home, "shim")
        os.makedirs(shim_dir)
        shim = os.path.join(shim_dir, "systemctl")
        with open(shim, "w") as handle:
            handle.write(SYSTEMCTL_SHIM)
        os.chmod(shim, 0o755)
        self.shim_dir = shim_dir
        self.systemctl_log = os.path.join(self.home, "systemctl.log")

    def run_setup(self, *args):
        env = dict(os.environ)
        env["HOME"] = self.home
        # config.py honours XDG_CONFIG_HOME before HOME. A developer session
        # may export it, but this real-setup fixture must never read or write
        # the developer's live Tonearm config.
        env.pop("XDG_CONFIG_HOME", None)
        env["PATH"] = self.shim_dir + os.pathsep + env["PATH"]
        env["SYSTEMCTL_LOG"] = self.systemctl_log
        return subprocess.run([os.path.join(self.plugin, "setup.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def systemctl_calls(self):
        if not os.path.exists(self.systemctl_log):
            return []
        with open(self.systemctl_log) as handle:
            return handle.read().splitlines()

    def config_path(self):
        return os.path.join(self.home, ".config", "tonearm", "config.json")

    def token_path(self):
        return os.path.join(self.home, ".config", "tonearm", "token")

    def temp_leftovers(self):
        if not os.path.isdir(self.unit_dir):
            return []
        return [n for n in os.listdir(self.unit_dir) if n != "tonearmd.service"]


class TestCleanInstall(SetupTestCase):
    def test_installs_the_unit(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(self.target))
        with open(self.target) as handle:
            self.assertIn("tonearmd", handle.read())

    def test_leaves_no_temp_file(self):
        self.run_setup()
        self.assertEqual(self.temp_leftovers(), [])

    def test_reinstalling_over_our_own_unit_succeeds(self):
        self.assertEqual(self.run_setup().returncode, 0)
        second = self.run_setup()
        self.assertEqual(second.returncode, 0, second.stderr)


class TestExplicitCoreBootstrap(SetupTestCase):
    def load_config(self):
        with open(self.config_path()) as handle:
            return json.load(handle)

    def test_explicit_core_uses_the_advertised_default_ports(self):
        result = self.run_setup("--core", "192.168.50.44")
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = self.load_config()
        self.assertEqual(cfg["host"], "192.168.50.44")
        self.assertEqual(cfg["http_port"], 9330)
        self.assertEqual(cfg["tcp_port"], 9150)

    def test_explicit_core_accepts_custom_ports(self):
        result = self.run_setup("--core", "roon-core.home",
                                "--http-port", "19331",
                                "--tcp-port", "19151")
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = self.load_config()
        # Deliberately unlike either default: ignoring an option cannot pass.
        self.assertEqual(cfg["http_port"], 19331)
        self.assertEqual(cfg["tcp_port"], 19151)

    def test_invalid_input_does_not_install_or_start_the_service(self):
        cases = [
            ("--core", ""),
            ("--core", "http://192.168.50.44"),
            ("--core", "192.168.50.44", "--http-port", "0"),
            ("--core", "192.168.50.44", "--tcp-port", "65536"),
            ("--core", "192.168.50.44", "--tcp-port", "not-a-port"),
        ]
        for args in cases:
            with self.subTest(args=args):
                result = self.run_setup(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(os.path.exists(self.target))
                self.assertFalse(os.path.exists(self.config_path()))
                self.assertEqual(self.systemctl_calls(), [])

    def test_ports_without_a_core_are_rejected_before_install(self):
        result = self.run_setup("--http-port", "19331")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.systemctl_calls(), [])

    def test_repeated_bootstrap_preserves_token_pin_and_identity(self):
        state_dir = os.path.dirname(self.config_path())
        os.makedirs(state_dir)
        with open(self.config_path(), "w") as handle:
            json.dump({"host": "192.168.50.44", "http_port": 9330,
                       "tcp_port": 9150, "name": "yavin",
                       "unique_id": "uid-yavin", "pinned_zone_id": "zone-7"},
                      handle)
        with open(self.token_path(), "w") as handle:
            handle.write("pairing-token")
        os.chmod(self.token_path(), 0o600)

        first = self.run_setup("--core", "192.168.50.44",
                               "--http-port", "19331")
        second = self.run_setup("--core", "192.168.50.44",
                                "--http-port", "19331")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        cfg = self.load_config()
        self.assertEqual(cfg["pinned_zone_id"], "zone-7")
        self.assertEqual(cfg["name"], "yavin")
        self.assertEqual(cfg["unique_id"], "uid-yavin")
        with open(self.token_path()) as handle:
            self.assertEqual(handle.read(), "pairing-token")
        self.assertEqual(os.stat(self.token_path()).st_mode & 0o077, 0)
        self.assertEqual(os.stat(self.config_path()).st_mode & 0o077, 0)
        self.assertEqual(os.stat(state_dir).st_mode & 0o077, 0)

    def test_a_different_configured_core_is_refused(self):
        state_dir = os.path.dirname(self.config_path())
        os.makedirs(state_dir)
        original = {"host": "192.168.50.44", "http_port": 9330,
                    "tcp_port": 9150, "name": "yavin",
                    "unique_id": "uid-yavin", "pinned_zone_id": "zone-7"}
        with open(self.config_path(), "w") as handle:
            json.dump(original, handle)

        result = self.run_setup("--core", "192.168.50.99")
        self.assertNotEqual(result.returncode, 0)
        with open(self.config_path()) as handle:
            self.assertEqual(json.load(handle), original)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.systemctl_calls(), [])


class TestRefusesToWriteThroughAPlant(SetupTestCase):
    def test_a_symlink_at_the_unit_path_is_refused(self):
        victim = os.path.join(self.home, "victim")
        with open(victim, "w") as handle:
            handle.write("untouched")
        os.makedirs(self.unit_dir)
        os.symlink(victim, self.target)

        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        with open(victim) as handle:
            self.assertEqual(handle.read(), "untouched")

    def test_an_unrelated_service_file_is_not_clobbered(self):
        os.makedirs(self.unit_dir)
        with open(self.target, "w") as handle:
            handle.write("[Service]\nExecStart=/usr/bin/something-else\n")

        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated", result.stderr)
        with open(self.target) as handle:
            self.assertIn("something-else", handle.read())

    def test_a_non_regular_file_is_refused(self):
        os.makedirs(self.unit_dir)
        os.mkfifo(self.target)
        self.addCleanup(os.unlink, self.target)

        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a regular file", result.stderr)

    def test_a_refusal_leaves_no_temp_file(self):
        os.makedirs(self.unit_dir)
        with open(self.target, "w") as handle:
            handle.write("[Service]\nExecStart=/usr/bin/something-else\n")
        self.run_setup()
        # The refusal happens before mktemp runs, so there is nothing to clean
        # up. temp_leftovers() already excludes the unit file itself.
        self.assertEqual(self.temp_leftovers(), [])


if __name__ == "__main__":
    unittest.main()


class TestSandboxPrerequisites(SetupTestCase):
    """The hardened unit's mount namespace must be satisfiable at first start.

    ProtectSystem=strict makes $HOME read-only, so the daemon can no longer
    create ~/.config/tonearm itself -- and systemd refuses to start a unit
    whose ReadWritePaths names a directory that does not exist, failing at
    step NAMESPACE with 226 before the process ever runs. Measured
    2026-09-02: a probe unit pointed at a missing path did exactly that.
    So the installer has to create it.
    """

    def state_dir(self):
        return os.path.join(self.home, ".config", "tonearm")

    def test_creates_the_state_directory(self):
        self.run_setup()
        self.assertTrue(os.path.isdir(self.state_dir()))

    def test_the_state_directory_is_private(self):
        # It holds the Roon pairing token.
        self.run_setup()
        self.assertEqual(os.stat(self.state_dir()).st_mode & 0o077, 0)

    def test_an_existing_loose_state_directory_is_tightened(self):
        os.makedirs(self.state_dir(), exist_ok=True)
        os.chmod(self.state_dir(), 0o755)
        self.run_setup()
        self.assertEqual(os.stat(self.state_dir()).st_mode & 0o077, 0)

    def test_every_readwritepath_in_the_unit_exists_after_setup(self):
        """The coupling, asserted rather than assumed.

        A ReadWritePaths line added to the unit without a matching mkdir in
        setup.sh does not degrade -- the unit refuses to start at all, with
        an error that names systemd rather than this plugin. This catches
        that at test time instead of on someone's first install.
        """
        self.run_setup()
        with open(self.target) as handle:
            unit = handle.read()
        paths = [line.split("=", 1)[1].strip()
                 for line in unit.splitlines()
                 if line.startswith("ReadWritePaths=")]
        self.assertTrue(paths, "the unit declares no ReadWritePaths")
        for raw in paths:
            for entry in raw.split():
                path = entry.lstrip("-").replace("%h", self.home)
                self.assertTrue(os.path.isdir(path),
                                "%s is in ReadWritePaths but setup.sh does "
                                "not create it" % entry)
