import json
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse, server


class StubSession:
    def __init__(self, reply=None, error=None):
        self.reply = reply or {"ok": True, "level_id": 3, "path": ["Search"],
                               "count": 0, "offset": 0, "rows": []}
        self.error = error
        self.seen = []

    def snapshot(self):
        return {"v": 1, "status": "ok"}

    def browse(self, key, op, **kwargs):
        self.seen.append((key, op, kwargs))
        if self.error:
            raise self.error
        return self.reply

    def command(self, verb, arg=None):
        pass


def roundtrip(session, request):
    """Serve exactly one connection and return the parsed reply."""
    srv = server.Server(session)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        conn, _ = listener.accept()
        client.sendall((json.dumps(request) + "\n").encode())
        thread = threading.Thread(target=srv._handle, args=(conn,))
        thread.start()
        line = client.makefile("r").readline()
        thread.join(timeout=5)
        client.close()
        listener.close()
    return json.loads(line) if line else None


class TestBrowseVerb(unittest.TestCase):
    def test_replies_with_the_level(self):
        s = StubSession()
        reply = roundtrip(s, {"cmd": "browse", "session": "widget",
                              "op": "search", "term": "oingo boingo"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["level_id"], 3)
        self.assertEqual(reply["v"], 1)

    def test_forwards_the_op_and_arguments(self):
        s = StubSession()
        roundtrip(s, {"cmd": "browse", "session": "widget", "op": "enter",
                      "index": 2, "level_id": 7})
        key, op, kwargs = s.seen[0]
        self.assertEqual(key, "widget")
        self.assertEqual(op, "enter")
        self.assertEqual(kwargs["index"], 2)
        self.assertEqual(kwargs["level_id"], 7)

    def test_a_missing_session_defaults_to_widget(self):
        s = StubSession()
        roundtrip(s, {"cmd": "browse", "op": "back"})
        self.assertEqual(s.seen[0][0], "widget")

    def test_a_browse_error_becomes_a_token_reply(self):
        s = StubSession(error=browse.BrowseError("stale", "out of date"))
        reply = roundtrip(s, {"cmd": "browse", "op": "enter", "index": 0})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "stale")
        self.assertEqual(reply["message"], "out of date")

    def test_an_unexpected_exception_becomes_roon_error_not_a_hang(self):
        s = StubSession(error=RuntimeError("boom"))
        reply = roundtrip(s, {"cmd": "browse", "op": "back"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "roon_error")

    def test_no_reply_ever_contains_an_item_key(self):
        # spec 5.3 invariant, asserted at the wire.
        s = StubSession(reply={
            "ok": True, "level_id": 1, "path": [], "count": 1, "offset": 0,
            "rows": [{"title": "X", "subtitle": "", "image_key": None,
                      "can_descend": True, "can_play": True}]})
        reply = roundtrip(s, {"cmd": "browse", "op": "back"})
        self.assertNotIn("item_key", json.dumps(reply))


class TestExistingVerbsStillWork(unittest.TestCase):
    def test_status_is_unaffected(self):
        s = StubSession()
        reply = roundtrip(s, {"cmd": "status"})
        self.assertEqual(reply["status"], "ok")
