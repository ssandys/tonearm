"""The player envelope over the wire: server route, CLI parse, client reply.

Synthetic sockets only -- a real server object on a temporary socket path,
and stub daemons that answer nothing or answer the wrong thing.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import art, cli, player, server   # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TONEARMCTL = os.path.join(ROOT, "scripts", "tonearmctl")


def request(**overrides) -> dict:
    envelope = {"cmd": "player_targeted", "v": 1, "request_id": "req-1",
                "zone_id": "zone-a", "verb": "play", "arg": None,
                "output_id": None, "output_ids": None}
    envelope.update(overrides)
    return envelope


class FakeSession:
    """A session that answers the player route and records what it was asked."""

    def __init__(self, outcome=("attempted", None)):
        self.commands = []
        self.player_calls = []
        self.outcome = outcome
        self.failure = None

    def snapshot(self):
        return {"v": 1, "status": "ok", "core": None, "zone": None, "zones": []}

    def command(self, verb, arg=None):
        self.commands.append((verb, arg))

    def player_targeted(self, zone_id, verb, arg, output_id, output_ids):
        self.player_calls.append((zone_id, verb, arg, output_id, output_ids))
        if self.failure is not None:
            raise self.failure
        return {"outcome": self.outcome[0], "error": self.outcome[1]}


class PlayerRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._runtime = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name
        self.addCleanup(self._restore_runtime)

    def _restore_runtime(self):
        # Put the previous value back rather than deleting the variable:
        # anything later in this process that resolves the daemon's socket
        # path must not fall back to the live runtime directory.
        if self._runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._runtime

    def _serve(self, session):
        srv = server.Server(session)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(srv.ready.wait(5), "server never started listening")
        self.addCleanup(self._stop, srv, thread)
        return srv

    @staticmethod
    def _stop(srv, thread):
        # The listener is closed AND its thread finished before the temporary
        # runtime directory goes away: a thread still inside accept() would
        # otherwise outlive the socket it was bound to.
        srv.shutdown()
        thread.join(3)
        assert not thread.is_alive(), "the server thread outlived its shutdown"

    def _round_trip(self, payload):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(server.socket_path())
        sock.sendall((json.dumps(payload) + "\n").encode())
        reader = sock.makefile("rb")
        line = reader.readline()
        sock.close()
        return line

    def test_the_route_answers_exactly_one_correlated_line(self):
        session = FakeSession()
        self._serve(session)
        line = self._round_trip(request())
        self.assertTrue(line.endswith(b"\n"))
        reply = json.loads(line)
        self.assertEqual(reply["request_id"], "req-1")
        self.assertEqual(reply["zone_id"], "zone-a")
        self.assertEqual(reply["verb"], "play")
        self.assertEqual(reply["outcome"], "attempted")
        self.assertIsNone(reply["error"])
        self.assertEqual(session.player_calls,
                         [("zone-a", "play", None, None, None)])

    def test_a_malformed_request_is_refused_without_dispatch(self):
        session = FakeSession()
        self._serve(session)
        for broken in (request(arg=True), request(verb="transfer"),
                       request(request_id=""), dict(request(), extra=1),
                       {"cmd": "player_targeted"}):
            with self.subTest(broken=broken):
                reply = json.loads(self._round_trip(broken))
                self.assertEqual(reply["outcome"], "refused")
                self.assertEqual(reply["error"], "bad_request")
        self.assertEqual(session.player_calls, [])

    def test_a_daemon_without_the_route_says_so(self):
        # Silence is the one thing a client cannot tell from a lost reply,
        # so an older daemon's refusal is spelled out.
        class OldSession:
            def snapshot(self):
                return {"v": 1, "status": "ok", "core": None, "zone": None,
                        "zones": []}

            def command(self, verb, arg=None):
                pass

        self._serve(OldSession())
        reply = json.loads(self._round_trip(request()))
        self.assertEqual(reply["outcome"], "refused")
        self.assertEqual(reply["error"], "unsupported")
        self.assertEqual(reply["request_id"], "req-1")

    def test_a_failing_session_is_unknown_not_refused(self):
        session = FakeSession()
        session.failure = RuntimeError("something went wrong downstream")
        self._serve(session)
        reply = json.loads(self._round_trip(request()))
        self.assertEqual(reply["outcome"], "unknown")
        self.assertEqual(reply["error"], "internal_error")
        self.assertEqual(reply["request_id"], "req-1")

    def test_a_nonsense_outcome_from_the_session_is_unknown(self):
        session = FakeSession(outcome=("refused", "not_a_real_code"))
        self._serve(session)
        reply = json.loads(self._round_trip(request()))
        self.assertEqual(reply["outcome"], "unknown")
        self.assertEqual(reply["error"], "internal_error")

    def test_the_legacy_commands_still_answer_with_nothing(self):
        # Unchanged: the plain verbs send and exit, and the daemon closes
        # without a reply line.
        session = FakeSession()
        self._serve(session)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(server.socket_path())
        sock.sendall(b'{"cmd":"playpause"}\n')
        self.assertEqual(sock.makefile("rb").readline(), b"")
        sock.close()
        self.assertEqual(session.commands, [("playpause", None)])
        self.assertEqual(session.player_calls, [])


class CliParseTest(unittest.TestCase):
    def test_the_player_verb_parses_one_envelope(self):
        envelope = request()
        self.assertEqual(cli.to_request(["player", json.dumps(envelope)]),
                         envelope)

    def test_the_player_verb_rejects_anything_it_cannot_send(self):
        for argv in (["player"],
                     ["player", "{", "extra"],
                     ["player", json.dumps(request(verb="transfer"))],
                     ["player", json.dumps(request(arg=True))],
                     ["player", json.dumps(request(request_id=""))],
                     ["player", json.dumps({"cmd": "playpause"})]):
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError):
                    cli.to_request(argv)


class StubDaemon:
    """A socket that answers whatever it is told to, or nothing at all.

    `reply` is the raw line to send, or None to close without answering --
    which is what a daemon that does not speak this protocol does.
    """

    def __init__(self, path, reply=None):
        self.path = path
        self.reply = reply
        self.requests = []
        self._stopped = False
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        # A poll timeout, so the accept loop notices a stop instead of
        # sitting in accept() past the end of the test.
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stopped:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                line = conn.makefile("rb").readline()
                self.requests.append(line)
                if self.reply is not None:
                    conn.sendall(self.reply)

    def stop(self):
        """Close the listener and let its thread finish."""
        self._stopped = True
        self.sock.close()
        self.thread.join(3)
        assert not self.thread.is_alive(), "the stub thread outlived its close"


class ResultFieldTest(unittest.TestCase):
    """Every field of a reply must be PRESENT, not merely absent-or-null.

    `error` is the one that matters: an "attempted" reply that omits the key
    reads as "no error" to a validator that reaches for it with .get(), while
    the widget's reader wants an explicit null and calls an omitted field
    unknown. The two readers disagreed, and the lenient one won the race to
    the wire. The reply's key set is exact, so a missing field is rejected.
    """

    def test_a_reply_with_every_field_is_a_result(self):
        reply = player.result(request(), "attempted", None)
        self.assertEqual(set(reply), set(player.RESULT_FIELDS))
        self.assertTrue(player.valid_result(reply, request()))

    def test_an_omitted_error_is_not_a_result(self):
        reply = player.result(request(), "attempted", None)
        del reply["error"]
        self.assertNotIn("error", reply)
        self.assertFalse(player.valid_result(reply, request()))

    def test_an_omitted_error_is_not_a_result_for_any_outcome(self):
        for outcome, error in (("attempted", None),
                               ("refused", "capability_unavailable"),
                               ("unknown", "dispatch_failed")):
            with self.subTest(outcome=outcome):
                reply = player.result(request(), outcome, error)
                del reply["error"]
                self.assertFalse(player.valid_result(reply, request()))

    def test_any_other_missing_or_extra_field_is_not_a_result(self):
        for field in ("v", "type", "request_id", "zone_id", "verb", "outcome"):
            with self.subTest(missing=field):
                reply = player.result(request(), "attempted", None)
                del reply[field]
                self.assertFalse(player.valid_result(reply, request()))
        reply = player.result(request(), "attempted", None)
        reply["surprise"] = "field"
        self.assertFalse(player.valid_result(reply, request()))


class ClientReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = os.path.join(self.tmp.name, "tonearm")
        os.makedirs(self.runtime)
        self.path = os.path.join(self.runtime, "sock")

    def _run(self, envelope):
        env = dict(os.environ, XDG_RUNTIME_DIR=self.tmp.name)
        return subprocess.run(
            [sys.executable, TONEARMCTL, "player", json.dumps(envelope)],
            capture_output=True, text=True, timeout=30, env=env, cwd=ROOT)

    def test_a_correlated_reply_is_printed_and_exits_zero(self):
        reply = player.result(request(), "attempted", None)
        daemon = StubDaemon(self.path, (json.dumps(reply) + "\n").encode())
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["outcome"], "attempted")
        self.assertEqual(len(daemon.requests), 1)

    def test_a_refusal_is_reported_and_exits_nonzero(self):
        reply = player.result(request(), "refused", "capability_unavailable")
        daemon = StubDaemon(self.path, (json.dumps(reply) + "\n").encode())
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout)["error"], "capability_unavailable")

    def test_a_reply_for_another_request_is_not_this_ones_answer(self):
        reply = player.result(request(request_id="other"), "attempted", None)
        daemon = StubDaemon(self.path, (json.dumps(reply) + "\n").encode())
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")

    def test_a_reply_for_another_room_is_not_this_ones_answer(self):
        reply = player.result(request(zone_id="zone-b"), "attempted", None)
        daemon = StubDaemon(self.path, (json.dumps(reply) + "\n").encode())
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")

    def test_an_empty_reply_is_an_unknown_effect_and_is_not_retried(self):
        daemon = StubDaemon(self.path, None)
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        # One request, one attempt. A second attempt after an uncertain
        # dispatch would be a second command.
        self.assertEqual(len(daemon.requests), 1)

    def test_a_malformed_reply_is_an_unknown_effect_and_is_not_retried(self):
        daemon = StubDaemon(self.path, b"this is not json\n")
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertEqual(len(daemon.requests), 1)

    def test_an_oversized_reply_is_refused(self):
        daemon = StubDaemon(self.path, b"x" * (256 * 1024 + 16) + b"\n")
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertEqual(len(daemon.requests), 1)

    def test_the_client_never_falls_back_to_the_legacy_verbs(self):
        # The stub records the raw request line: it must be the player
        # envelope, once, and nothing else.
        reply = player.result(request(), "unknown", "dispatch_failed")
        daemon = StubDaemon(self.path, (json.dumps(reply) + "\n").encode())
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(len(daemon.requests), 1)
        self.assertEqual(json.loads(daemon.requests[0])["cmd"], "player_targeted")

    def test_a_reply_without_an_explicit_error_is_an_unknown_effect(self):
        # The daemon said "attempted" but left the field out. The client
        # cannot read that as "no error", so the effect is unknown: it says
        # so, exits nonzero, and does not send a second command.
        reply = player.result(request(), "attempted", None)
        del reply["error"]
        daemon = StubDaemon(self.path, (json.dumps(reply) + "\n").encode())
        self.addCleanup(daemon.stop)
        done = self._run(request())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        # One request, one attempt: a retry after an uncertain dispatch would
        # be a second command.
        self.assertEqual(len(daemon.requests), 1)


class EndToEndTest(unittest.TestCase):
    """The reference client, the real daemon, one correlated command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._runtime = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name
        self.addCleanup(self._restore_runtime)

    def _restore_runtime(self):
        # The previous value goes back, not a deletion: a later test in this
        # process must not resolve the daemon's socket path into the live
        # runtime directory.
        if self._runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._runtime

    def _serve(self, session):
        """Run a server on the temporary socket, and stop it before cleanup.

        The listener is closed and its thread joined while the temporary
        runtime directory is still in place: `shutdown()` unlinks the socket
        file, and that path has to be the temporary one.
        """
        srv = server.Server(session)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(srv.ready.wait(5), "server never started listening")
        self.addCleanup(self._stop, srv, thread)
        return srv

    @staticmethod
    def _stop(srv, thread):
        srv.shutdown()
        thread.join(3)
        assert not thread.is_alive(), "the server thread outlived its shutdown"

    def test_the_reference_client_and_the_daemon_agree(self):
        session = FakeSession()
        self._serve(session)
        env = dict(os.environ, XDG_RUNTIME_DIR=self.tmp.name)
        done = subprocess.run(
            [sys.executable, TONEARMCTL, "player", json.dumps(request())],
            capture_output=True, text=True, timeout=30, env=env, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        reply = json.loads(done.stdout)
        self.assertEqual(reply["outcome"], "attempted")
        self.assertEqual(reply["request_id"], "req-1")
        self.assertEqual(session.player_calls,
                         [("zone-a", "play", None, None, None)])
        # The plain verbs were never used: one request, one command.
        self.assertEqual(session.commands, [])

    def test_the_route_survives_the_daemons_session_wrapper(self):
        # scripts/tonearmd hands the server an art.CachingSession, not the
        # raw session: a route the wrapper does not forward is unreachable,
        # and the answer would be "unsupported" for every request.
        session = FakeSession()
        wrapped = art.CachingSession(session, art.Cache(self.tmp.name))
        self._serve(wrapped)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(server.socket_path())
        sock.sendall((json.dumps(request()) + "\n").encode())
        reply = json.loads(sock.makefile("rb").readline())
        sock.close()
        self.assertEqual(reply["outcome"], "attempted")
        self.assertEqual(session.player_calls,
                         [("zone-a", "play", None, None, None)])


if __name__ == "__main__":
    unittest.main()
