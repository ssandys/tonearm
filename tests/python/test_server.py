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

        # The racer blocks on this subscriber's own send lock until the
        # handshake finishes, NOT on Server._lock -- which the handshake no
        # longer holds across snapshot() or the write. Ordering is the
        # property under test; which lock enforces it is not.
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


# --- hardening: marketplace security review 2026-09-01 -------------------
#
# "The thread-per-client Unix socket has no connection cap or read/write
# deadline, and subscriber `sendall()` can block while the global lock is
# held."


class _StalledPeer:
    """A subscriber whose sendall() blocks until released.

    A real peer that stops reading needs ~200 KiB of queued broadcast before
    its socket buffer fills, which makes the timing of the property under
    test depend on the kernel's default wmem. This blocks on command
    instead, the same way the existing handshake-race test above blocks
    snapshot() on command rather than hoping timing exposes the bug.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def sendall(self, blob):
        self.entered.set()
        self.release.wait(5)

    def settimeout(self, timeout):
        pass

    def close(self):
        pass


class TestServerBounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name
        # Restore every bound this class lowers, so a failure mid-test cannot
        # leave a 0.3s request deadline in place for the rest of the suite.
        for name in ("REQUEST_TIMEOUT", "MAX_CONNECTIONS", "MAX_SUBSCRIBERS"):
            self.addCleanup(setattr, server, name, getattr(server, name))
        self.session = FakeSession()

    def _server(self):
        srv = server.Server(self.session)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        for _ in range(200):
            if os.path.exists(server.socket_path()):
                break
            time.sleep(0.01)
        self.addCleanup(srv.shutdown)
        return srv

    def _connect(self, timeout=5):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(server.socket_path())
        self.addCleanup(sock.close)
        return sock

    def test_a_silent_client_is_hung_up_on_rather_than_pinning_a_thread(self):
        # A client that connects and never sends a newline held its handler
        # thread in readline() forever. No packet is larger than any other,
        # and nothing ever timed it out -- so N such connections cost N
        # permanently parked threads.
        server.REQUEST_TIMEOUT = 0.3
        self._server()
        sock = self._connect()
        started = time.monotonic()
        self.assertEqual(sock.recv(4096), b"")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_concurrent_connections_are_capped(self):
        # accept() spawned a thread per connection with no ceiling. Past the
        # cap the daemon must refuse immediately rather than keep spawning.
        server.REQUEST_TIMEOUT = 30.0     # the held slots must not just expire
        server.MAX_CONNECTIONS = 2
        self._server()
        held = [self._connect() for _ in range(2)]
        for sock in held:
            sock.sendall(b'{"cmd": "sub')  # a partial line: parks in readline()
        time.sleep(0.2)

        overflow = self._connect()
        started = time.monotonic()
        self.assertEqual(overflow.recv(4096), b"")
        self.assertLess(time.monotonic() - started, 3.0,
                        "the overflow connection was queued, not refused")

    def test_subscribers_are_capped(self):
        server.MAX_SUBSCRIBERS = 2
        self._server()
        for _ in range(2):
            sock = self._connect()
            sock.sendall(b'{"cmd":"subscribe"}\n')
            self.assertIn('"status"', sock.makefile("r").readline())

        overflow = self._connect()
        overflow.sendall(b'{"cmd":"subscribe"}\n')
        self.assertEqual(overflow.makefile("r").readline(), "")

    def test_a_non_object_request_is_refused_like_malformed_input(self):
        # `3\n` is valid JSON but not a request. request.get() then raised
        # AttributeError, which no handler caught: the thread died WITHOUT
        # closing the connection, leaking the socket and leaving the client
        # blocked on a reply that never came.
        self._server()
        for body in (b"3\n", b'"subscribe"\n', b"[1,2]\n", b"null\n"):
            sock = self._connect()
            sock.sendall(body)
            self.assertEqual(sock.recv(4096), b"", "not closed for %r" % body)

    def test_a_stalled_subscriber_does_not_block_a_new_subscribe(self):
        # broadcast() serialized the payload before taking the lock, but then
        # ran sendall() INSIDE it -- so one peer that had stopped reading
        # stalled every other subscriber and every new subscribe for as long
        # as it took to time out. The send must happen outside the lock.
        srv = self._server()
        stalled = _StalledPeer()
        srv._subscribers.append(server._Subscriber(stalled))

        broadcaster = threading.Thread(
            target=srv.broadcast, args=({"v": 1, "status": "connecting"},))
        broadcaster.start()
        self.addCleanup(stalled.release.set)
        self.addCleanup(broadcaster.join, 5)
        self.assertTrue(stalled.entered.wait(3), "broadcast never reached the peer")

        # The stalled peer is mid-sendall. A fresh subscribe must still
        # complete: it needs the subscriber list, which nothing is holding.
        sock = self._connect()
        sock.sendall(b'{"cmd":"subscribe"}\n')
        self.assertIn('"status"', sock.makefile("r").readline())

    def test_a_stalled_subscriber_does_not_block_delivery_to_a_healthy_one(self):
        srv = self._server()
        healthy = self._connect()
        healthy.sendall(b'{"cmd":"subscribe"}\n')
        reader = healthy.makefile("r")
        reader.readline()                       # the initial snapshot

        stalled = _StalledPeer()
        srv._subscribers.insert(0, server._Subscriber(stalled))
        self.addCleanup(stalled.release.set)

        broadcaster = threading.Thread(
            target=srv.broadcast, args=({"v": 1, "status": "unreachable"},))
        broadcaster.start()
        self.addCleanup(broadcaster.join, 5)
        self.assertTrue(stalled.entered.wait(3))

        # Ahead of the healthy peer in the list, and still blocked. The
        # healthy peer must be served anyway.
        self.assertEqual(json.loads(reader.readline())["status"], "unreachable")


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


if __name__ == "__main__":
    unittest.main()


class TestServerFailureIsVisible(unittest.TestCase):
    """A dead socket server must become a dead process.

    The server runs on a daemon=True thread, so its death is invisible:
    the asyncio loop in scripts/tonearmd keeps running, systemd still sees
    an active unit, and Restart=on-failure never fires. Measured under a
    hardened unit on 2026-09-02 -- an OSError on the bind path left the
    process `active`, NRestarts 0, and no socket on disk. The bar would sit
    on a stale track forever with nothing retrying, which is the exact
    outcome systemd/tonearmd.service's own StartLimitIntervalSec comment
    calls worse than a visible failure.

    MPRIS is deliberately NOT load-bearing (scripts/tonearmd catches
    everything around it). The socket is the daemon's entire purpose, so
    the opposite rule applies to it.
    """

    class _Boom:
        running = True

        def serve_forever(self):
            raise OSError("bind failed")

    class _ReturnsEarly:
        running = True

        def serve_forever(self):
            return

    class _StoppedDeliberately:
        running = False

        def serve_forever(self):
            return

    def test_a_crashing_server_reports_failure(self):
        called = []
        server.supervise(self._Boom(), lambda: called.append(True))
        self.assertEqual(called, [True])

    def test_a_server_that_returns_while_running_reports_failure(self):
        # accept() falling out of the loop without shutdown() is just as
        # dead as a raised exception, and was just as silent.
        called = []
        server.supervise(self._ReturnsEarly(), lambda: called.append(True))
        self.assertEqual(called, [True])

    def test_a_deliberate_shutdown_reports_nothing(self):
        # shutdown() closes the listener, so accept() raises and
        # serve_forever returns -- on the ordinary SIGTERM path. That must
        # not be reported as a failure or every clean stop exits non-zero
        # and systemd restarts a daemon the user just stopped.
        called = []
        server.supervise(self._StoppedDeliberately(), lambda: called.append(True))
        self.assertEqual(called, [])

    def test_a_real_server_shutting_down_reports_nothing(self):
        # The fakes above assert the contract; this asserts the real Server
        # actually satisfies it, so `running` cannot drift from shutdown().
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_RUNTIME_DIR"] = tmp
            srv = server.Server(FakeSession())
            called = []
            thread = threading.Thread(
                target=server.supervise, args=(srv, lambda: called.append(True)))
            thread.start()
            for _ in range(200):
                if os.path.exists(server.socket_path()):
                    break
                time.sleep(0.01)
            srv.shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(called, [])
