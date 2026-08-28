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
