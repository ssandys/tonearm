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

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

SYSTEMCTL_SHIM = "#!/bin/sh\nexit 0\n"


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

        self.unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        self.target = os.path.join(self.unit_dir, "tonearmd.service")

        shim_dir = os.path.join(self.home, "shim")
        os.makedirs(shim_dir)
        shim = os.path.join(shim_dir, "systemctl")
        with open(shim, "w") as handle:
            handle.write(SYSTEMCTL_SHIM)
        os.chmod(shim, 0o755)
        self.shim_dir = shim_dir

    def run_setup(self):
        env = dict(os.environ)
        env["HOME"] = self.home
        env["PATH"] = self.shim_dir + os.pathsep + env["PATH"]
        return subprocess.run([os.path.join(self.plugin, "setup.sh")],
                              env=env, capture_output=True, text=True, timeout=60)

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
