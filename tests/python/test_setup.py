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
# `busctl --user status NAME` is how --check asks whether MPRIS is registered.
# BUSCTL_RC decides the answer; every call is logged like systemctl's.
BUSCTL_SHIM = ("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BUSCTL_LOG\"\n"
               "exit \"${BUSCTL_RC:-0}\"\n")

INSTALL_CALLS = ["--user daemon-reload", "--user enable tonearmd.service",
                 "--user restart tonearmd.service"]
UNINSTALL_CALLS = ["--user disable --now tonearmd.service",
                   "--user stop tonearmd-bus.service", "--user daemon-reload"]


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
        self.bus_target = os.path.join(self.unit_dir, "tonearmd-bus.service")

        shim_dir = os.path.join(self.home, "shim")
        os.makedirs(shim_dir)
        shim = os.path.join(shim_dir, "systemctl")
        with open(shim, "w") as handle:
            handle.write(SYSTEMCTL_SHIM)
        os.chmod(shim, 0o755)
        busctl = os.path.join(shim_dir, "busctl")
        with open(busctl, "w") as handle:
            handle.write(BUSCTL_SHIM)
        os.chmod(busctl, 0o755)
        self.shim_dir = shim_dir
        self.systemctl_log = os.path.join(self.home, "systemctl.log")
        self.busctl_log = os.path.join(self.home, "busctl.log")
        self.busctl_rc = 0
        # The real /usr/bin/xdg-dbus-proxy may or may not exist on the machine
        # running the suite; every test decides for itself.
        self.use_proxy(present=True)

    def use_proxy(self, present):
        """Point the fixture's setup.sh at a fake proxy binary, or at none.

        Same technique as test_uninstall_does_not_require_python_dependencies:
        only the copied fixture script changes.
        """
        fake = os.path.join(self.home, "bin", "xdg-dbus-proxy")
        os.makedirs(os.path.dirname(fake), exist_ok=True)
        if present and not os.path.exists(fake):
            with open(fake, "w") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake, 0o755)
        elif not present and os.path.exists(fake):
            os.unlink(fake)
        script = os.path.join(self.plugin, "setup.sh")
        with open(script) as handle:
            lines = handle.read().splitlines(keepends=True)
        marker = "readonly proxy_bin="
        hits = [i for i, line in enumerate(lines) if line.startswith(marker)]
        self.assertEqual(len(hits), 1, "setup.sh must define proxy_bin once")
        lines[hits[0]] = marker + fake + "\n"
        with open(script, "w") as handle:
            handle.writelines(lines)

    def run_setup(self, *args):
        env = dict(os.environ)
        env["HOME"] = self.home
        # config.py honours XDG_CONFIG_HOME before HOME. A developer session
        # may export it, but this real-setup fixture must never read or write
        # the developer's live Tonearm config.
        env.pop("XDG_CONFIG_HOME", None)
        env["PATH"] = self.shim_dir + os.pathsep + env["PATH"]
        env["SYSTEMCTL_LOG"] = self.systemctl_log
        env["BUSCTL_LOG"] = self.busctl_log
        env["BUSCTL_RC"] = str(self.busctl_rc)
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
        return [n for n in os.listdir(self.unit_dir)
                if n not in ("tonearmd.service", "tonearmd-bus.service")]


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
        self.assertFalse(os.path.lexists(self.bus_target))
        self.assertEqual(self.systemctl_calls(), UNINSTALL_CALLS)
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
        self.assertEqual(self.systemctl_calls(), UNINSTALL_CALLS)

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

    # Every directive whose source path must EXIST when the unit starts.
    # ReadWritePaths was the only one until #42 replaced it with binds; all
    # three fail the same way, at step NAMESPACE, before the process runs.
    PATH_DIRECTIVES = ("ReadWritePaths=", "BindPaths=", "BindReadOnlyPaths=")

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
        paths = [(line.split("=", 1)[0], line.split("=", 1)[1].strip())
                 for line in unit.splitlines()
                 if line.startswith(self.PATH_DIRECTIVES)]
        self.assertTrue(paths, "the unit declares no path directives at all")
        for directive, raw in paths:
            for entry in raw.split():
                # A `-` prefix is systemd's own "skip if missing", which is
                # the opposite of this hazard: the proxy directory is bound
                # that way precisely so a missing xdg-dbus-proxy cannot keep
                # the daemon from starting (test_session_bus_proxy.py).
                if entry.startswith("-"):
                    continue
                # A bind may be `source:dest`; the source is what must exist.
                source = entry.split(":")[0]
                path = source.replace("%h", self.home)
                self.assertTrue(os.path.isdir(path),
                                "%s is in %s but setup.sh does not create it"
                                % (entry, directive))


class TestBusProxyUnit(SetupTestCase):
    """setup.sh installs tonearmd-bus.service next to tonearmd.service.

    Same guards as the daemon's unit -- it is written to the same directory by
    the same script -- and both are checked before EITHER is written, so a
    refusal cannot leave a daemon unit that wants a proxy nobody installed.
    """

    def test_installs_both_units(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.bus_target) as handle:
            installed = handle.read()
        with open(os.path.join(REPO, "systemd", "tonearmd-bus.service")) as handle:
            self.assertEqual(installed, handle.read())
        self.assertEqual(os.stat(self.bus_target).st_mode & 0o777, 0o644)
        self.assertEqual(self.temp_leftovers(), [])

    def test_rerunning_restarts_the_daemon_so_unit_changes_apply(self):
        # `enable --now` starts a stopped unit and does nothing to a running
        # one, so a re-run never applied a changed unit -- 0.14.2's upgrade
        # note had to tell users to restart by hand. The proxy needs no call
        # of its own: tonearmd Wants= it, and its PartOf= follows the restart.
        self.assertEqual(self.run_setup().returncode, 0)
        os.unlink(self.systemctl_log)
        second = self.run_setup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.systemctl_calls(), INSTALL_CALLS)

    def test_a_symlink_at_the_proxy_unit_path_is_refused_before_any_write(self):
        victim = os.path.join(self.home, "victim")
        with open(victim, "w") as handle:
            handle.write("untouched")
        os.makedirs(self.unit_dir)
        os.symlink(victim, self.bus_target)

        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        with open(victim) as handle:
            self.assertEqual(handle.read(), "untouched")
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.systemctl_calls(), [])

    def test_a_non_regular_proxy_unit_is_refused(self):
        os.makedirs(self.unit_dir)
        os.mkfifo(self.bus_target)
        self.addCleanup(os.unlink, self.bus_target)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a regular file", result.stderr)
        self.assertFalse(os.path.exists(self.target))

    def test_an_unrelated_proxy_unit_is_not_clobbered(self):
        os.makedirs(self.unit_dir)
        with open(self.bus_target, "w") as handle:
            handle.write("[Service]\nExecStart=/usr/bin/something-else\n")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated", result.stderr)
        with open(self.bus_target) as handle:
            self.assertIn("something-else", handle.read())
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.temp_leftovers(), [])

    def test_a_missing_proxy_warns_but_still_installs(self):
        # The widget does not need the bus; only media keys do.
        self.use_proxy(present=False)
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("xdg-dbus-proxy", result.stderr)
        self.assertIn("media keys", result.stderr)
        self.assertTrue(os.path.isfile(self.target))
        self.assertTrue(os.path.isfile(self.bus_target))
        self.assertEqual(self.systemctl_calls(), INSTALL_CALLS)

    def test_a_present_proxy_does_not_warn(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("xdg-dbus-proxy", result.stderr)

    def test_setup_checks_the_binary_the_unit_runs(self):
        with open(os.path.join(REPO, "setup.sh")) as handle:
            [line] = [l for l in handle if l.startswith("readonly proxy_bin=")]
        proxy_bin = line.split("=", 1)[1].strip()
        with open(os.path.join(REPO, "systemd", "tonearmd-bus.service")) as handle:
            unit = handle.read()
        self.assertIn("ExecStart=" + proxy_bin + " ", unit)
        self.assertIn("ConditionFileIsExecutable=" + proxy_bin + "\n", unit)


class TestUninstallBusProxy(SetupTestCase):
    def test_uninstalls_an_older_install_without_the_proxy_unit(self):
        # Everything before 0.14.3 installed tonearmd.service alone.
        self.assertEqual(self.run_setup().returncode, 0)
        os.unlink(self.bus_target)
        os.unlink(self.systemctl_log)
        result = self.run_setup("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(self.systemctl_calls(), [
            "--user disable --now tonearmd.service", "--user daemon-reload"])

    def test_removes_a_proxy_unit_left_without_its_daemon(self):
        self.assertEqual(self.run_setup().returncode, 0)
        os.unlink(self.target)
        os.unlink(self.systemctl_log)
        result = self.run_setup("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.lexists(self.bus_target))
        self.assertEqual(self.systemctl_calls(), [
            "--user stop tonearmd-bus.service", "--user daemon-reload"])

    def test_refuses_an_unowned_proxy_unit_before_stopping_anything(self):
        for kind in ("symlink", "fifo", "directory", "unrelated"):
            with self.subTest(kind=kind):
                self.assertEqual(self.run_setup().returncode, 0)
                os.unlink(self.systemctl_log)
                os.unlink(self.bus_target)
                if kind == "symlink":
                    victim = os.path.join(self.home, "victim")
                    with open(victim, "w") as handle:
                        handle.write("untouched")
                    os.symlink(victim, self.bus_target)
                elif kind == "fifo":
                    os.mkfifo(self.bus_target)
                elif kind == "directory":
                    os.mkdir(self.bus_target)
                else:
                    with open(self.bus_target, "w") as handle:
                        handle.write("[Service]\nExecStart=/usr/bin/other\n")

                result = self.run_setup("--uninstall")
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(os.path.lexists(self.bus_target))
                # The daemon's own unit is untouched too: nothing was stopped.
                self.assertTrue(os.path.isfile(self.target))
                self.assertEqual(self.systemctl_calls(), [])
                if kind == "symlink":
                    with open(victim) as handle:
                        self.assertEqual(handle.read(), "untouched")
                    os.unlink(victim)
                if kind == "directory":
                    os.rmdir(self.bus_target)
                else:
                    os.unlink(self.bus_target)


class TestCheckReportsMediaKeys(SetupTestCase):
    """--check says whether media keys can work, and why not if they cannot.

    Not a failure exit: the widget works without MPRIS, the same way an
    unpaired daemon is reported but exits 0. It must never say "healthy"
    while media keys are dead, which is how 0.14.0's regression went unseen.
    """

    def setUp(self):
        super().setUp()
        ctl = os.path.join(self.plugin, "scripts", "tonearmctl")
        with open(ctl, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(ctl, 0o755)
        state = os.path.join(self.home, ".config", "tonearm")
        os.makedirs(state)
        with open(os.path.join(state, "token"), "w") as handle:
            handle.write("pairing-token")

    def busctl_calls(self):
        if not os.path.exists(self.busctl_log):
            return []
        with open(self.busctl_log) as handle:
            return handle.read().splitlines()

    def test_healthy_when_mpris_is_registered(self):
        result = self.run_setup("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("healthy", result.stdout)
        self.assertIn("Media keys: org.mpris.MediaPlayer2.tonearm is registered",
                      result.stdout)
        self.assertIn("--user status org.mpris.MediaPlayer2.tonearm",
                      " ".join(self.busctl_calls()))

    def test_reports_a_missing_proxy_binary(self):
        self.use_proxy(present=False)
        result = self.run_setup("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("healthy", result.stdout)
        self.assertIn("xdg-dbus-proxy is not installed", result.stdout)
        self.assertIn("omarchy pkg add xdg-dbus-proxy", result.stdout)

    def test_reports_an_unregistered_name(self):
        self.busctl_rc = 1
        result = self.run_setup("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("healthy", result.stdout)
        self.assertIn("not registered", result.stdout)
        self.assertIn("tonearmd-bus", result.stdout)

    def test_unpaired_still_reports_media_keys(self):
        os.unlink(os.path.join(self.home, ".config", "tonearm", "token"))
        self.busctl_rc = 1
        result = self.run_setup("--check")
        self.assertIn("NOT PAIRED", result.stdout)
        self.assertIn("not registered", result.stdout)
