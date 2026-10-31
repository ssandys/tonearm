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
import socket
import stat
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
        self.runtime = os.path.join(self.home, "runtime")
        os.mkdir(self.runtime, 0o700)

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
        env["XDG_RUNTIME_DIR"] = self.runtime
        # config.py honours XDG_CONFIG_HOME before HOME. A developer session
        # may export it, but this real-setup fixture must never read or write
        # the developer's live Tonearm config.
        env.pop("XDG_CONFIG_HOME", None)
        env["PATH"] = self.shim_dir + os.pathsep + env["PATH"]
        env["SYSTEMCTL_LOG"] = self.systemctl_log
        return subprocess.run([os.path.join(self.plugin, "setup.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def assert_mount_sources_exist(self, unit):
        paths = [(line.split("=", 1)[0], line.split("=", 1)[1].strip())
                 for line in unit.splitlines()
                 if line.startswith(("ReadWritePaths=", "BindPaths=", "BindReadOnlyPaths="))]
        self.assertTrue(paths, "the unit declares no path directives at all")
        for directive, raw in paths:
            for entry in raw.split():
                # A '-' makes a missing source optional. Bind sources can be
                # files/sockets, not just directories; %t is the user runtime.
                optional = entry.startswith("-")
                source = entry.removeprefix("-").split(":")[0]
                path = source.replace("%h", self.home).replace("%t", self.runtime)
                self.assertNotIn("%", path, "unhandled unit specifier")
                if optional and not os.path.exists(path):
                    continue
                self.assertTrue(os.path.exists(path),
                                "%s is in %s but its required source is absent"
                                % (entry, directive))
                if directive == "ReadWritePaths":
                    self.assertTrue(os.path.isdir(path))


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


class TestUninstall(SetupTestCase):
    def test_removes_owned_unit_in_order_and_preserves_pairing(self):
        self.assertEqual(self.run_setup().returncode, 0)
        state_dir = os.path.join(self.home, ".config", "tonearm")
        token = os.path.join(state_dir, "token")
        with open(token, "w") as handle:
            handle.write("pairing-token")
        os.chmod(token, 0o600)
        os.unlink(self.systemctl_log)

        result = self.run_setup("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(self.systemctl_calls(), [
            "--user disable --now tonearmd.service", "--user daemon-reload"])
        with open(token) as handle:
            self.assertEqual(handle.read(), "pairing-token")
        self.assertEqual(os.stat(state_dir).st_mode & 0o077, 0)

    def test_uninstall_twice_is_idempotent(self):
        self.assertEqual(self.run_setup().returncode, 0)
        self.assertEqual(self.run_setup("--uninstall").returncode, 0)
        os.unlink(self.systemctl_log)
        second = self.run_setup("--uninstall")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.systemctl_calls(), [])

    def test_uninstall_without_a_unit_does_not_touch_systemd(self):
        result = self.run_setup("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.systemctl_calls(), [])
        self.assertFalse(os.path.exists(self.unit_dir))

    def test_refuses_unowned_or_non_regular_unit_before_stopping_service(self):
        for kind in ("symlink", "fifo", "directory", "unrelated"):
            with self.subTest(kind=kind):
                os.makedirs(self.unit_dir, exist_ok=True)
                if kind == "symlink":
                    victim = os.path.join(self.home, "victim")
                    with open(victim, "w") as handle:
                        handle.write("untouched")
                    os.symlink(victim, self.target)
                elif kind == "fifo":
                    os.mkfifo(self.target)
                elif kind == "directory":
                    os.mkdir(self.target)
                else:
                    with open(self.target, "w") as handle:
                        handle.write("[Service]\nExecStart=/usr/bin/other\n")

                result = self.run_setup("--uninstall")
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(os.path.lexists(self.target))
                self.assertEqual(self.systemctl_calls(), [])
                if kind == "symlink":
                    with open(victim) as handle:
                        self.assertEqual(handle.read(), "untouched")
                    os.unlink(victim)
                if kind == "directory":
                    os.rmdir(self.target)
                else:
                    os.unlink(self.target)

    def test_uninstall_with_extra_argument_is_a_usage_error(self):
        self.assertEqual(self.run_setup().returncode, 0)
        os.unlink(self.systemctl_log)
        result = self.run_setup("--uninstall", "extra")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(os.path.isfile(self.target))
        self.assertEqual(self.systemctl_calls(), [])

    def test_uninstall_does_not_require_python_dependencies(self):
        self.assertEqual(self.run_setup().returncode, 0)
        os.unlink(self.systemctl_log)
        # Only the copied fixture script is changed. If uninstall reaches the
        # dependency probe, this deliberately missing Python path makes it fail.
        script = os.path.join(self.plugin, "setup.sh")
        with open(script) as handle:
            source = handle.read()
        source = source.replace("/usr/bin/python -c", "/does-not-exist/python -c")
        with open(script, "w") as handle:
            handle.write(source)
        result = self.run_setup("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.systemctl_calls(), [
            "--user disable --now tonearmd.service", "--user daemon-reload"])

    def test_unit_skips_start_when_daemon_has_been_removed(self):
        self.assertEqual(self.run_setup().returncode, 0)
        with open(self.target) as handle:
            unit = handle.read()
        self.assertIn(
            "ConditionPathExists=%h/.config/omarchy/plugins/"
            "ssandys.tonearm/scripts/tonearmd", unit)



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

    def test_every_bound_path_in_the_unit_exists_after_setup(self):
        """The coupling, asserted rather than assumed.

        A path directive added to the unit without a matching mkdir in
        setup.sh does not degrade -- the unit refuses to start at all, with
        an error that names systemd rather than this plugin. This catches
        that at test time instead of on someone's first install.

        Widened from ReadWritePaths alone in #42, which swapped that for
        BindPaths and BindReadOnlyPaths to get $HOME hidden rather than
        merely read-only. The guard caught the swap, which is what it is for,
        and the requirement it protects did not change: systemd will not
        start a unit whose bind source is missing either.
        """
        self.run_setup()
        with open(self.target) as handle:
            unit = handle.read()
        self.assert_mount_sources_exist(unit)


class TestSessionBusPackaging(SetupTestCase):
    def installed_unit(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.target) as handle:
            return handle.read()

    def test_exposes_only_optional_session_bus_with_hardening_retained(self):
        unit = self.installed_unit()
        self.assertIn("BindReadOnlyPaths=-%t/bus", unit.splitlines())
        for directive in ("ProtectHome=tmpfs", "ProtectSystem=strict",
                          "PrivateTmp=yes", "PrivateDevices=yes",
                          "NoNewPrivileges=yes", "UMask=0077"):
            self.assertIn(directive, unit.splitlines())
        binds = [line for line in unit.splitlines()
                 if line.startswith(("BindPaths=", "BindReadOnlyPaths="))]
        self.assertEqual(binds, [
            "BindReadOnlyPaths=%h/.config/omarchy/plugins/ssandys.tonearm",
            "BindReadOnlyPaths=-%t/bus", "BindPaths=%h/.config/tonearm"])

    def test_missing_bus_is_not_created_or_owned_by_setup(self):
        self.installed_unit()
        self.assertFalse(os.path.lexists(os.path.join(self.runtime, "bus")))
        self.assertEqual(self.run_setup("--uninstall").returncode, 0)
        self.assertEqual(os.listdir(self.runtime), [])

    def test_existing_socket_survives_install_reinstall_and_uninstall(self):
        bus = os.path.join(self.runtime, "bus")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as fixture:
            fixture.bind(bus)  # A disposable socket, never a session-bus client.
            before = os.stat(bus)
            unit = self.installed_unit()
            self.assert_mount_sources_exist(unit)
            self.installed_unit()
            self.assertEqual(self.run_setup("--uninstall").returncode, 0)
            after = os.stat(bus)
            self.assertTrue(stat.S_ISSOCK(after.st_mode))
            self.assertEqual((after.st_ino, after.st_mode),
                             (before.st_ino, before.st_mode))

    def test_path_guard_allows_optional_absence_but_rejects_required_absence(self):
        self.assert_mount_sources_exist("BindReadOnlyPaths=-%t/missing")
        with self.assertRaises(AssertionError):
            self.assert_mount_sources_exist("BindReadOnlyPaths=%t/missing")
        with self.assertRaises(AssertionError):
            self.assert_mount_sources_exist("BindReadOnlyPaths=-%unknown/path")

    def test_existing_bus_and_user_dropins_are_preserved(self):
        directory = self.target + ".d"
        os.makedirs(directory)
        contents = {
            "80-mpris-bus.conf": "[Service]\nBindReadOnlyPaths=-%t/bus\n",
            "90-user.conf": "[Service]\nEnvironment=USER_CHOICE=kept\n"}
        for name, text in contents.items():
            path = os.path.join(directory, name)
            with open(path, "w") as handle:
                handle.write(text)
            os.chmod(path, 0o600)
        self.installed_unit()
        self.installed_unit()
        self.assertEqual(self.run_setup("--uninstall").returncode, 0)
        self.assertEqual(sorted(os.listdir(directory)), sorted(contents))
        for name, text in contents.items():
            path = os.path.join(directory, name)
            with open(path) as handle:
                self.assertEqual(handle.read(), text)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_symlinked_user_dropin_directory_is_not_traversed_or_removed(self):
        os.makedirs(self.unit_dir)
        external = os.path.join(self.home, "user-overrides")
        os.mkdir(external)
        marker = os.path.join(external, "90-user.conf")
        with open(marker, "w") as handle:
            handle.write("[Service]\nEnvironment=USER_CHOICE=kept\n")
        os.symlink(external, self.target + ".d")
        self.installed_unit()
        self.assertEqual(self.run_setup("--uninstall").returncode, 0)
        self.assertTrue(os.path.islink(self.target + ".d"))
        with open(marker) as handle:
            self.assertEqual(handle.read(), "[Service]\nEnvironment=USER_CHOICE=kept\n")
