"""tonearmd reaches the session bus only through a filtering proxy.

0.14.0 set `ProtectHome=tmpfs`, which also hides /run/user/$UID -- so the
session bus socket vanished from the sandbox and MPRIS (media keys) silently
died. Binding %t/bus back in would fix that and hand a daemon that parses
untrusted LAN bytes the WHOLE session bus: org.freedesktop.systemd1 included,
whose StartTransientUnit runs anything, outside every directive in this unit.

So the daemon gets a socket served by xdg-dbus-proxy, whose policy is: own
exactly the MPRIS name, see and talk to nothing else. These tests pin that
policy and the plumbing between the two units, because every way of getting it
wrong either fails silently (no media keys) or fails open (full bus).
"""

import os
import shlex
import sys
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from tonearm_lib import mpris  # noqa: E402

DAEMON_UNIT = os.path.join(REPO, "systemd", "tonearmd.service")
PROXY_UNIT = os.path.join(REPO, "systemd", "tonearmd-bus.service")
PROXY_BIN = "/usr/bin/xdg-dbus-proxy"
PROXY_SOCKET = "%t/tonearm-bus/bus"

PATH_DIRECTIVES = ("BindPaths", "BindReadOnlyPaths", "ReadWritePaths",
                   "ReadOnlyPaths")


def parse(path):
    """(section, key, value) for every directive, comments dropped."""
    out, section = [], None
    with open(path) as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            key, _, value = line.partition("=")
            out.append((section, key.strip(), value.strip()))
    return out


def values(directives, key, section=None):
    return [v for s, k, v in directives
            if k == key and (section is None or s == section)]


def bound_sources(directives):
    """Every path a directive exposes, as written (prefix and dest kept off)."""
    sources = []
    for key in PATH_DIRECTIVES:
        for value in values(directives, key):
            for entry in value.split():
                sources.append(entry.lstrip("-+").split(":")[0])
    return sources


class TestDaemonUnit(unittest.TestCase):
    def setUp(self):
        self.unit = parse(DAEMON_UNIT)

    def test_the_session_bus_itself_is_never_bound_in(self):
        # The rejected fix. Any spelling of it -- %t/bus, the runtime dir as a
        # whole, a literal /run/user path -- is full session-bus access.
        for source in bound_sources(self.unit):
            with self.subTest(source=source):
                self.assertNotIn(source.rstrip("/"), ("%t/bus", "%t"))
                self.assertFalse(source.startswith("/run/user"), source)

    def test_the_only_runtime_path_bound_in_is_the_proxy_directory(self):
        runtime = [s for s in bound_sources(self.unit) if s.startswith("%t")]
        self.assertEqual(runtime, ["%t/tonearm-bus"])

    def test_the_proxy_directory_is_read_only_and_optional(self):
        # Read-only: the daemon connects to the socket, it never creates one.
        # Optional (`-`): without xdg-dbus-proxy the directory never exists,
        # and a missing bind source fails the WHOLE unit at step NAMESPACE.
        # MPRIS is not load-bearing; the daemon must start regardless.
        self.assertIn("-%t/tonearm-bus", values(self.unit, "BindReadOnlyPaths"))
        for key in ("BindPaths", "ReadWritePaths"):
            for value in values(self.unit, key):
                self.assertNotIn("tonearm-bus", value)

    def test_dbus_next_is_pointed_at_the_proxy(self):
        self.assertIn(
            "DBUS_SESSION_BUS_ADDRESS=unix:path=" + PROXY_SOCKET,
            values(self.unit, "Environment", "Service"))

    def test_the_proxy_is_wanted_and_ordered_but_not_required(self):
        wants = " ".join(values(self.unit, "Wants", "Unit")).split()
        after = " ".join(values(self.unit, "After", "Unit")).split()
        self.assertIn("tonearmd-bus.service", wants)
        self.assertIn("tonearmd-bus.service", after)
        # Requires/BindsTo would take the daemon -- the widget, not just the
        # media keys -- down with a missing or failed proxy.
        for key in ("Requires", "Requisite", "BindsTo", "PartOf"):
            self.assertNotIn("tonearmd-bus", " ".join(values(self.unit, key)),
                             key)

    def test_home_stays_hidden(self):
        self.assertEqual(values(self.unit, "ProtectHome"), ["tmpfs"])


class TestProxyUnit(unittest.TestCase):
    def setUp(self):
        self.unit = parse(PROXY_UNIT)
        [execstart] = values(self.unit, "ExecStart", "Service")
        self.argv = shlex.split(execstart)

    def test_the_policy_is_exactly_own_the_mpris_name(self):
        # --filter with no --talk/--see: the daemon sees only the bus driver
        # and its own name. --own implies talk for that one name, which is
        # all MPRIS needs: consumers' calls INTO us are relayed, and our
        # PropertiesChanged broadcasts go out. Verified live, see CHANGELOG.
        self.assertEqual(self.argv, [
            PROXY_BIN, "unix:path=%t/bus", PROXY_SOCKET,
            "--filter", "--own=" + mpris.BUS_NAME])

    def test_the_owned_name_is_the_one_the_daemon_requests(self):
        # Not a wildcard: `NAME.*` would let the daemon own a whole subtree.
        self.assertEqual(mpris.BUS_NAME, "org.mpris.MediaPlayer2.tonearm")
        owns = [a for a in self.argv if a.startswith("--own")]
        self.assertEqual(owns, ["--own=org.mpris.MediaPlayer2.tonearm"])

    def test_no_policy_option_widens_access(self):
        for arg in self.argv:
            with self.subTest(arg=arg):
                self.assertFalse(arg.startswith(
                    ("--talk", "--see", "--call", "--broadcast",
                     "--sloppy-names", "--log")), arg)

    def test_the_socket_lands_in_its_own_private_runtime_directory(self):
        self.assertEqual(values(self.unit, "RuntimeDirectory"), ["tonearm-bus"])
        self.assertEqual(values(self.unit, "RuntimeDirectoryMode"), ["0700"])
        self.assertEqual(os.path.dirname(PROXY_SOCKET), "%t/tonearm-bus")

    def test_a_missing_proxy_binary_skips_the_unit_rather_than_failing_it(self):
        # xdg-dbus-proxy arrives with webkit2gtk-4.1 on stock Omarchy but is
        # not guaranteed. A condition makes its absence a clean skip, not a
        # failed unit with a restart loop.
        self.assertIn(PROXY_BIN, values(self.unit, "ConditionFileIsExecutable"))

    def test_it_is_ready_only_once_exec_succeeds(self):
        # Type=simple would release the daemon's After= at fork, before
        # RuntimeDirectory exists, and the optional bind would silently skip.
        self.assertEqual(values(self.unit, "Type"), ["exec"])

    def test_its_lifetime_follows_the_daemon(self):
        # Stopping or restarting tonearmd stops or restarts the proxy too, so
        # re-running setup.sh (which restarts tonearmd) refreshes both.
        self.assertIn("tonearmd.service", values(self.unit, "PartOf", "Unit"))

    def test_it_cannot_be_enabled_on_its_own(self):
        # Pulled in only by tonearmd's Wants=; no [Install], so nothing can
        # leave a bus proxy running without the daemon it serves.
        self.assertEqual([s for s, _, _ in self.unit if s == "Install"], [])

    def test_it_sees_only_the_session_bus_socket(self):
        self.assertEqual(values(self.unit, "ProtectHome"), ["tmpfs"])
        self.assertEqual(bound_sources(self.unit), ["%t/bus"])

    def test_it_has_no_network(self):
        self.assertEqual(values(self.unit, "PrivateNetwork"), ["yes"])
        self.assertEqual(values(self.unit, "RestrictAddressFamilies"),
                         ["AF_UNIX"])

    def test_it_is_otherwise_sandboxed_like_the_daemon(self):
        for key, value in (("NoNewPrivileges", "yes"),
                           ("ProtectSystem", "strict"),
                           ("PrivateTmp", "yes"),
                           ("PrivateDevices", "yes"),
                           ("MemoryDenyWriteExecute", "yes"),
                           ("RestrictNamespaces", "yes"),
                           ("SystemCallArchitectures", "native"),
                           ("UMask", "0077")):
            with self.subTest(key=key):
                self.assertEqual(values(self.unit, key), [value])
        self.assertIn("@system-service", values(self.unit, "SystemCallFilter"))


if __name__ == "__main__":
    unittest.main()
