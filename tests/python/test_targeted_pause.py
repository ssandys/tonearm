"""Production-session/IPC slice with synthetic rooms; no Core or audio."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.python.test_selection_identity import session, zone
from tonearm_lib import server, art


def request(zid="a", rid="test-1"):
    return {"cmd": "pause_targeted", "v": 1, "request_id": rid, "zone_id": zid}


class TargetedPauseTest(unittest.TestCase):
    def setUp(self):
        self.s = session("a", zones=[zone("a"), zone("b", "playing")], generation=1)
        self.api = self.s._api

    def test_pause_binds_id_without_legacy_command_dispatch(self):
        with patch.object(self.s, "command", side_effect=AssertionError("legacy path")):
            result = self.s.pause_targeted("a")
        self.assertEqual(result, {"outcome": "attempted", "error": None})
        self.assertEqual(self.api.calls, [("a", "pause")])

    def test_refusals_do_not_retarget_or_dispatch(self):
        for case, error in [("disconnect", "disconnected"), ("socket", "disconnected"),
                            ("not_ready", "zones_unknown"), ("missing", "unavailable"),
                            ("changed", "selection_changed")]:
            with self.subTest(case=case):
                s = session("a", zones=[zone("a"), zone("b", "playing")], generation=1)
                if case == "disconnect": s._status = "unreachable"
                if case == "socket": s._api._roonsocket.connected = False
                if case == "not_ready": s._api._zone_list_generation = 0
                if case == "missing": del s._api.zones["a"]
                if case == "changed": s._arbiter.pin("b")
                self.assertEqual(s.pause_targeted("a"), {"outcome": "refused", "error": error})
                self.assertEqual(s._api.calls, [])

    def test_legacy_fallback_setting_cannot_redirect_explicit_request(self):
        s = session("a", strict=False, zones=[zone("b", "playing")], generation=1)
        self.assertEqual(s.snapshot()["zone"]["id"], "b")
        self.assertEqual(s.pause_targeted("a")["error"], "unavailable")
        self.assertEqual(s._api.calls, [])

    def test_auto_selection_must_match_current_target(self):
        s = session(zones=[zone("a", "playing"), zone("b")], generation=1)
        self.assertEqual(s.pause_targeted("b")["error"], "selection_changed")
        self.assertEqual(s._api.calls, [])
        self.assertEqual(s.pause_targeted("a")["outcome"], "attempted")
        self.assertEqual(s._api.calls, [("a", "pause")])

    def test_sdk_exception_is_unknown_and_not_retried(self):
        def dispatch(zid, verb):
            self.api.calls.append((zid, verb))
            raise RuntimeError("private exception detail")
        self.api.playback_control = dispatch
        result = self.s.pause_targeted("a")
        self.assertEqual(result, {"outcome": "unknown", "error": "dispatch_failed"})
        self.assertEqual(self.api.calls, [("a", "pause")])
        self.assertNotIn("private", json.dumps(result))

    def test_dispatch_retains_bound_api_and_id_if_local_state_changes(self):
        def dispatch(zid, verb):
            self.s._api = None
            self.s._arbiter.pin("b")
            self.api.calls.append((zid, verb))
        self.api.playback_control = dispatch
        self.assertEqual(self.s.pause_targeted("a")["outcome"], "attempted")
        self.assertEqual(self.api.calls, [("a", "pause")])

    def test_invalid_direct_target_is_refused(self):
        for zid in (None, "", [], True, "a" * 129):
            self.assertEqual(self.s.pause_targeted(zid)["error"], "bad_request")
        self.assertEqual(self.api.calls, [])

    def exchange(self, payload):
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        client.settimeout(3)
        srv = server.Server(self.s)
        worker = threading.Thread(target=srv._handle, args=(peer,))
        worker.start()
        try:
            client.sendall(json.dumps(payload).encode() + b"\n")
            with client.makefile("rb") as reader:
                line = reader.readline()
                self.assertEqual(reader.readline(), b"")
            return json.loads(line)
        finally:
            client.close()
            worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_wire_result_is_correlated_and_legacy_status_is_compatible(self):
        reply = self.exchange(request())
        self.assertEqual(reply, {"v": 1, "type": "command_result", "request_id": "test-1",
                                 "zone_id": "a", "outcome": "attempted", "error": None})
        snapshot = self.exchange({"cmd": "status"})
        self.assertEqual(snapshot["v"], 1)
        self.assertEqual(snapshot["selection"]["zone_id"], "a")
        self.assertTrue({"v", "status", "core", "zone", "zones"} <= snapshot.keys())

    def test_malformed_envelopes_never_dispatch(self):
        for change in ({"v": True}, {"v": 2}, {"request_id": ""}, {"request_id": []},
                       {"request_id": "r" * 65}, {"zone_id": []}, {"zone_id": ""},
                       {"zone_id": "z" * 129}, {"arg": "play"}):
            with self.subTest(change=change):
                reply = self.exchange(dict(request(), **change))
                self.assertEqual(reply["outcome"], "refused")
                self.assertEqual(reply["error"], "bad_request")
        self.assertEqual(self.api.calls, [])

    def test_missing_fields_and_unsupported_session_fail_closed(self):
        for key in ("v", "request_id", "zone_id"):
            payload = request()
            del payload[key]
            reply = self.exchange(payload)
            self.assertEqual(reply["error"], "bad_request")
        original = self.s
        self.s = object()  # no opt-in method; never calls legacy command()
        self.assertEqual(self.exchange(request())["error"], "unsupported")
        self.s = original
        self.assertEqual(self.api.calls, [])

    def test_session_fault_returns_unknown_without_exception_text(self):
        with patch.object(self.s, "pause_targeted", side_effect=RuntimeError("secret")):
            reply = self.exchange(request())
        self.assertEqual(reply["outcome"], "unknown")
        self.assertEqual(reply["error"], "internal_error")
        self.assertNotIn("secret", json.dumps(reply))

    def test_wire_refusal_echoes_request_identity(self):
        self.s._arbiter.pin("b")
        reply = self.exchange(request())
        self.assertEqual(reply["request_id"], "test-1")
        self.assertEqual(reply["zone_id"], "a")
        self.assertEqual(reply["error"], "selection_changed")
        self.assertEqual(self.api.calls, [])


class ContractClientTest(unittest.TestCase):
    def test_no_reply_or_wrong_correlation_never_falls_back(self):
        from tonearm_lib import contract_client
        for response in (b"", b'{"v":1,"status":"ok"}\n',
                         json.dumps({"v": 1, "type": "command_result", "request_id": "other",
                                     "zone_id": "a", "outcome": "attempted", "error": None}).encode()+b"\n"):
            sent = []
            class Peer:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def settimeout(self, value): pass
                def connect(self, path): pass
                def sendall(self, data): sent.append(json.loads(data))
                def makefile(self, *args):
                    import io
                    return io.BytesIO(response)
            with patch.object(contract_client.socket, "socket", return_value=Peer()):
                with self.assertRaises(contract_client.ContractError):
                    contract_client.pause("unused-test-socket", "a", "test-1")
            self.assertEqual(sent, [request()])

    def test_tiny_client_uses_actual_existing_daemon_socket(self):
        from tonearm_lib import contract_client
        s = session("a", zones=[zone("a")], generation=1)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            srv = server.Server(art.CachingSession(s, art.Cache(tmp)))
            worker = threading.Thread(target=srv.serve_forever)
            worker.start()
            try:
                self.assertTrue(srv.ready.wait(3))
                snapshot = contract_client.status(server.socket_path())
                self.assertEqual(snapshot["selection"]["zone_id"], "a")
                self.assertEqual(s._api.calls, [])
                env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]/"scripts"))
                proc = subprocess.run([sys.executable, "-m", "tonearm_lib.contract_client",
                                       "--socket", server.socket_path(), "--pause-zone", "a"],
                                      env=env, capture_output=True, text=True, timeout=8)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                lines = [json.loads(line) for line in proc.stdout.splitlines()]
                self.assertEqual(lines[0]["selection"]["zone_id"], "a")
                self.assertEqual(lines[1]["outcome"], "attempted")
                self.assertEqual(s._api.calls, [("a", "pause")])
                del s._api.zones["a"]
                reply = contract_client.pause(server.socket_path(), "a", "removed")
                self.assertEqual(reply["error"], "unavailable")
                self.assertEqual(s._api.calls, [("a", "pause")])
            finally:
                srv.shutdown()
                worker.join(3)
                self.assertFalse(worker.is_alive())
