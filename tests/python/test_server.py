import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import server


class FakeSession:
    def __init__(self):
        self.commands = []
        self.status = "ok"

    def snapshot(self):
        return {"v": 1, "status": "ok", "core": None, "zone": None, "zones": []}

    def command(self, verb, arg=None):
        self.commands.append((verb, arg))


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name
        self.session = FakeSession()
        self.srv = server.Server(self.session)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        for _ in range(100):
            if os.path.exists(server.socket_path()):
                break
            time.sleep(0.01)
        self.addCleanup(self.srv.shutdown)

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(server.socket_path())
        return sock

    def test_a_subscriber_gets_the_current_state_immediately(self):
        # Without this the widget renders nothing until the next Roon event,
        # which for a paused zone could be never.
        sock = self._connect()
        sock.sendall(b'{"cmd":"subscribe"}\n')
        line = sock.makefile("r").readline()
        self.assertEqual(json.loads(line)["status"], "ok")
        sock.close()

    def test_a_broadcast_racing_a_subscribe_handshake_is_never_lost(self):
        # Regression: the original design sent the initial reply before
        # registering the connection in self._subscribers. A broadcast
        # landing in that gap would iterate the subscriber list without
        # this connection in it yet and silently drop it -- readline() for
        # the "real" first update would then hang until socket timeout, no
        # error anywhere. Force the race deterministically (via a snapshot()
        # that blocks until told to proceed) rather than hoping timing
        # exposes it.
        entered_snapshot = threading.Event()
        release_snapshot = threading.Event()
        real_snapshot = self.session.snapshot

        def blocking_snapshot():
            entered_snapshot.set()
            release_snapshot.wait(3)
            return real_snapshot()

        self.session.snapshot = blocking_snapshot

        sock = self._connect()
        sock.sendall(b'{"cmd":"subscribe"}\n')

        self.assertTrue(entered_snapshot.wait(3), "handler never reached snapshot()")

        # Racer blocks on Server._lock until the handshake (reply + register)
        # finishes -- that is the property under test.
        racer = threading.Thread(
            target=self.srv.broadcast, args=({"v": 1, "status": "racing"},))
        racer.start()

        release_snapshot.set()
        racer.join(3)
        self.assertFalse(racer.is_alive(), "broadcast() never returned")

        reader = sock.makefile("r")
        first = json.loads(reader.readline())
        second = json.loads(reader.readline())
        self.assertEqual(first["status"], "ok")       # the initial snapshot
        self.assertEqual(second["status"], "racing")  # the racing broadcast, not lost
        sock.close()

    def test_broadcast_reaches_every_subscriber(self):
        a, b = self._connect(), self._connect()
        for sock in (a, b):
            sock.sendall(b'{"cmd":"subscribe"}\n')
            sock.makefile("r").readline()          # drain the initial snapshot
        self.srv.broadcast({"v": 1, "status": "unreachable"})
        for sock in (a, b):
            self.assertEqual(json.loads(sock.makefile("r").readline())["status"],
                             "unreachable")
        a.close(); b.close()

    def test_status_gets_one_reply_line_and_the_connection_closes(self):
        # setup.sh --check depends on this: connect, send status, read exactly
        # one line, and the server closes the connection afterward (unlike
        # subscribe, which stays open).
        sock = self._connect()
        sock.sendall(b'{"cmd":"status"}\n')
        reader = sock.makefile("r")
        line = reader.readline()
        self.assertEqual(json.loads(line)["status"], "ok")
        # The server closed its end; a second read returns EOF (empty string),
        # not a hang.
        self.assertEqual(reader.readline(), "")
        sock.close()

    def test_a_command_is_forwarded_and_the_connection_closes(self):
        sock = self._connect()
        sock.sendall(b'{"cmd":"seek","arg":42}\n')
        self.assertEqual(sock.makefile("r").readline().strip(), "")
        sock.close()
        for _ in range(100):
            if self.session.commands:
                break
            time.sleep(0.01)
        self.assertEqual(self.session.commands, [("seek", 42)])

    def test_malformed_input_does_not_kill_the_server(self):
        bad = self._connect()
        bad.sendall(b"not json at all\n")
        bad.close()
        good = self._connect()
        good.sendall(b'{"cmd":"subscribe"}\n')
        self.assertEqual(json.loads(good.makefile("r").readline())["status"], "ok")
        good.close()

    def test_a_dead_subscriber_does_not_block_broadcast(self):
        dead, live = self._connect(), self._connect()
        for sock in (dead, live):
            sock.sendall(b'{"cmd":"subscribe"}\n')
            sock.makefile("r").readline()
        dead.close()
        self.srv.broadcast({"v": 1, "status": "connecting"})
        self.assertEqual(json.loads(live.makefile("r").readline())["status"], "connecting")
        live.close()

    def test_a_stale_socket_file_is_replaced(self):
        # systemd may restart the daemon after a SIGKILL, which leaves the node
        # behind and would otherwise make bind() fail forever.
        self.srv.shutdown()
        with open(server.socket_path(), "w") as handle:
            handle.write("")
        srv = server.Server(FakeSession())
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        for _ in range(100):
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(server.socket_path())
                sock.close()
                break
            except OSError:
                time.sleep(0.01)
        else:
            self.fail("server never came up over a stale socket file")
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()


class TestRequestLineIsBounded(unittest.TestCase):
    """A client that never sends a newline must not exhaust the daemon.

    `conn.makefile("r").readline()` with no size argument reads until a
    newline arrives or memory runs out. The socket is 0600 in the user's own
    runtime dir, so this is a robustness bound rather than a privilege
    boundary -- but the marketplace review of a sibling plugin
    (HANCORE-linux/omarchy-plugin-marketplace#2659) treated "unbounded
    consumption from a predictable local path" as a finding on its own terms,
    in a directory that was equally user-owned.
    """

    def _server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prev = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmp.name

        def restore():
            if prev is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prev
        self.addCleanup(restore)

        srv = server.Server(FakeSession())
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.shutdown)
        for _ in range(200):
            if os.path.exists(server.socket_path()):
                break
            time.sleep(0.01)
        return srv

    def test_an_endless_line_is_refused_rather_than_buffered(self):
        self._server()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(server.socket_path())
        self.addCleanup(sock.close)

        # Well past the cap, and never a newline.
        chunk = b"x" * 65536
        sent = 0
        try:
            while sent < server.MAX_REQUEST_BYTES * 3:
                sock.sendall(chunk)
                sent += len(chunk)
        except OSError:
            pass  # the daemon hung up early, which is the desired outcome

        # The daemon hung up rather than accumulating. Either outcome proves
        # that: a clean EOF if it closed after our last send, or ECONNRESET if
        # it closed while we were still writing -- which is the usual race and
        # is just as much a refusal. What must NOT happen is this blocking.
        sock.settimeout(5)
        try:
            self.assertEqual(sock.recv(4096), b"")
        except ConnectionResetError:
            pass

    def test_an_ordinary_request_still_works(self):
        self._server()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(server.socket_path())
        self.addCleanup(sock.close)
        sock.sendall(b'{"cmd": "status"}\n')
        reply = sock.makefile("r").readline()
        self.assertIn('"status"', reply)
