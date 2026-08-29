import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CTL = os.path.join(ROOT, "scripts", "tonearmctl")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from tonearm_lib import cli   # noqa: E402


class TestArgParsing(unittest.TestCase):
    def test_a_bare_verb_becomes_a_command_object(self):
        self.assertEqual(cli.to_request(["playpause"]), {"cmd": "playpause"})

    def test_status_becomes_a_bare_command_object(self):
        # Added beyond the original brief: setup.sh --check (Task 13) calls
        # `tonearmctl status` as its health probe.
        self.assertEqual(cli.to_request(["status"]), {"cmd": "status"})

    def test_seek_and_volume_carry_an_integer_argument(self):
        self.assertEqual(cli.to_request(["seek", "42"]), {"cmd": "seek", "arg": 42})
        self.assertEqual(cli.to_request(["volume", "62"]), {"cmd": "volume", "arg": 62})

    def test_zone_pin_carries_the_id_as_a_string(self):
        self.assertEqual(cli.to_request(["zone", "pin", "z1"]),
                         {"cmd": "zone", "arg": "z1"})
        self.assertEqual(cli.to_request(["zone", "unpin"]),
                         {"cmd": "zone", "arg": "unpin"})

    def test_transfer_carries_the_destination_id(self):
        self.assertEqual(cli.to_request(["transfer", "z2"]),
                         {"cmd": "transfer", "arg": "z2"})

    def test_transfer_without_a_destination_is_a_usage_error(self):
        # Not a silent no-op: `transfer` alone has no sensible default
        # destination, and defaulting to anything would move audio into a room
        # the user never named.
        with self.assertRaises(ValueError):
            cli.to_request(["transfer"])

    def test_transfer_keeps_the_id_a_string(self):
        # Roon zone ids are opaque strings ("1601abc"). A numeric-looking one
        # must not be coerced the way seek/volume arguments are.
        self.assertEqual(cli.to_request(["transfer", "1601"]),
                         {"cmd": "transfer", "arg": "1601"})

    def test_a_non_numeric_seek_is_rejected_rather_than_sent(self):
        with self.assertRaises(ValueError):
            cli.to_request(["seek", "later"])

    def test_an_unknown_verb_is_rejected(self):
        with self.assertRaises(ValueError):
            cli.to_request(["frobnicate"])

    def test_no_arguments_is_rejected(self):
        with self.assertRaises(ValueError):
            cli.to_request([])


class TestExitCodes(unittest.TestCase):
    def test_subscribe_exits_3_when_the_daemon_is_absent(self):
        # Service.qml distinguishes "daemon down" from "bad usage" by this code,
        # and backs off rather than respawning in a tight loop.
        env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            env["XDG_RUNTIME_DIR"] = tmp
            proc = subprocess.run([CTL, "subscribe"], env=env,
                                  capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 3)

    def test_status_exits_3_when_the_daemon_is_absent(self):
        # setup.sh --check (Task 13) relies on this to detect a dead daemon.
        env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            env["XDG_RUNTIME_DIR"] = tmp
            proc = subprocess.run([CTL, "status"], env=env,
                                  capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 3)

    def test_a_bad_verb_exits_2(self):
        proc = subprocess.run([CTL, "frobnicate"], capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 2)


class TestStatusVerb(unittest.TestCase):
    """`status` reads exactly one line and exits, unlike `subscribe`'s stream.

    These stand up a bare unix-socket stub rather than tonearm_lib.server.Server:
    the merged server.py (Task 10) does not yet special-case "status" with a
    reply -- that lands in Task 13 alongside setup.sh --check. This suite pins
    down tonearmctl's own contract (send request, read one line, print it,
    exit 0) independent of that still-pending server-side change.
    """

    def _stub_socket_path(self, tmp):
        # Mirrors tonearm_lib.server.runtime_dir()/socket_path(): <base>/tonearm/sock.
        directory = os.path.join(tmp, "tonearm")
        os.makedirs(directory)
        return os.path.join(directory, "sock")

    def test_status_prints_the_reply_and_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = self._stub_socket_path(tmp)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)

            def respond():
                conn, _ = srv.accept()
                conn.makefile("r").readline()  # drain the request line
                conn.sendall(b'{"v":1,"status":"ok"}\n')
                conn.close()

            thread = threading.Thread(target=respond, daemon=True)
            thread.start()

            env = dict(os.environ)
            env["XDG_RUNTIME_DIR"] = tmp
            proc = subprocess.run([CTL, "status"], env=env,
                                  capture_output=True, timeout=15)
            thread.join(5)
            srv.close()

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b'{"v":1,"status":"ok"}\n')


class TestSubscribeIsUnbuffered(unittest.TestCase):
    """scripts/tonearmctl:56-61 flushes stdout after every `subscribe` line so
    QML's SplitParser -- which reads line by line -- sees each state update
    as it happens, instead of batched into a silent burst whenever Python's
    own stdout buffer happens to fill or the process exits. Nothing exercised
    a live multi-line stream before this test existed: deleting the
    `.flush()` call left the whole suite green.

    The stub below sequences two lines with a threading.Event and holds the
    second one until the test has proven the first is already sitting on the
    child's stdout pipe (checked via a non-blocking `select()`, not just
    "eventually read it"). That "arrived before the next was ever sent"
    ordering is reachable only if tonearmctl flushed line 1 through rather
    than buffering it. A test that merely asserted both lines show up after
    the daemon disconnects would pass with buffering fully intact and prove
    nothing -- see the fix report for empirical confirmation (flush()
    commented out -> this test fails; restored -> it passes).
    """

    def _stub_socket_path(self, tmp):
        # Mirrors tonearm_lib.server.runtime_dir()/socket_path(): <base>/tonearm/sock.
        directory = os.path.join(tmp, "tonearm")
        os.makedirs(directory)
        return os.path.join(directory, "sock")

    def test_each_line_arrives_before_the_next_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = self._stub_socket_path(tmp)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)

            first_read = threading.Event()   # set once line 1 is off the child's stdout
            second_sent = threading.Event()  # set once the stub has written line 2

            def stub():
                conn, _ = srv.accept()
                conn.makefile("r").readline()  # drain the subscribe request
                conn.sendall(b'{"seq":1}\n')
                # Gate: never send line 2 until the test proves line 1 already
                # reached the child's stdout -- see the class docstring.
                if first_read.wait(5):
                    conn.sendall(b'{"seq":2}\n')
                    second_sent.set()
                conn.close()  # EOF ends tonearmctl's read loop

            thread = threading.Thread(target=stub, daemon=True)
            thread.start()

            env = dict(os.environ)
            env["XDG_RUNTIME_DIR"] = tmp
            proc = subprocess.Popen([CTL, "subscribe"], env=env,
                                     stdout=subprocess.PIPE, text=True)
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 5)
                self.assertTrue(ready, "line 1 never reached stdout within 5s "
                                        "-- is flush() missing?")
                self.assertEqual(proc.stdout.readline(), '{"seq":1}\n')
                first_read.set()

                self.assertTrue(second_sent.wait(5), "stub never sent line 2")

                ready, _, _ = select.select([proc.stdout], [], [], 5)
                self.assertTrue(ready, "line 2 never reached stdout within 5s "
                                        "-- is flush() missing?")
                self.assertEqual(proc.stdout.readline(), '{"seq":2}\n')

                self.assertEqual(proc.wait(timeout=5), 3)  # daemon closed on us
            finally:
                proc.kill()
                proc.wait()
                proc.stdout.close()
                srv.close()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
