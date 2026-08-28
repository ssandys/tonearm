"""Local cover-art cache: fetch a small copy so ColorQuantizer can read it.

Measured in a live Quickshell probe: `ColorQuantizer` pointed at the Core's
remote `:9330/api/image/<key>` URL emits zero colors; pointed at the
identical bytes as a local `file://` it emits eight. Without a local copy the
album-art accent silently does nothing -- always falling back to the theme
accent, with no error anywhere. This module is what closes that gap: it
fetches into $XDG_RUNTIME_DIR/tonearm/art/<image_key>.jpg and hands back a
path, never a rendering decision.
"""

from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import art

IMAGE_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class _ImageHandler(http.server.BaseHTTPRequestHandler):
    """A tiny stand-in for the Core's /api/image/<key> endpoint."""

    def do_GET(self):  # noqa: N802 (stdlib method name)
        if self.path.startswith("/api/image/missing"):
            self.send_response(404)
            self.end_headers()
            return
        if self.path.startswith("/api/image/slow"):
            time.sleep(0.3)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()
        self.wfile.write(IMAGE_BYTES)

    def log_message(self, *_args):  # silence request logging in test output
        pass


class ArtServerTestCase(unittest.TestCase):
    """Base class that spins up a local HTTP server standing in for a Core."""

    def setUp(self):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _ImageHandler)
        self.host, self.port = self.httpd.server_address
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class TestFetch(ArtServerTestCase):
    def test_fetches_bytes_to_the_destination_path(self):
        dest = os.path.join(self.tmp.name, "art", "abc123.jpg")
        ok = art.fetch(self.host, self.port, "abc123", dest)
        self.assertTrue(ok)
        with open(dest, "rb") as handle:
            self.assertEqual(handle.read(), IMAGE_BYTES)

    def test_a_404_reports_failure_and_writes_nothing(self):
        dest = os.path.join(self.tmp.name, "art", "missing.jpg")
        ok = art.fetch(self.host, self.port, "missing", dest)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))

    def test_an_unreachable_host_reports_failure_rather_than_raising(self):
        dest = os.path.join(self.tmp.name, "art", "x.jpg")
        # Port 1 is reserved and nothing is listening there.
        ok = art.fetch("127.0.0.1", 1, "x", dest)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))


class TestCache(ArtServerTestCase):
    def _cache(self, on_ready=None):
        return art.Cache(self.tmp.name, on_ready=on_ready)

    def _drain(self, cache):
        # Join whatever background fetch `get()` last kicked off, so the test
        # method never returns -- and TemporaryDirectory.cleanup() never
        # runs -- while that thread might still be touching the same tree.
        # Polling for the destination file only proves the file landed, not
        # that the thread's trailing _prune() call has also finished.
        thread = cache._last_thread
        if thread is not None:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "background fetch never finished")

    def test_no_image_key_returns_none(self):
        cache = self._cache()
        self.assertIsNone(cache.get(self.host, self.port, None))

    def test_no_host_returns_none(self):
        cache = self._cache()
        self.assertIsNone(cache.get(None, self.port, "abc123"))

    def test_first_call_returns_none_and_does_not_block(self):
        cache = self._cache()
        started = time.time()
        result = cache.get(self.host, self.port, "slow")
        self.assertIsNone(result)
        self.assertLess(time.time() - started, 0.2)  # the fetch itself sleeps 0.3s
        self._drain(cache)

    def test_a_ready_path_is_returned_on_a_later_call(self):
        cache = self._cache()
        cache.get(self.host, self.port, "abc123")
        path = None
        for _ in range(100):
            path = cache.get(self.host, self.port, "abc123")
            if path:
                break
            time.sleep(0.01)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith("abc123.jpg"))
        self._drain(cache)

    def test_on_ready_fires_after_a_successful_fetch(self):
        fired = threading.Event()
        cache = self._cache(on_ready=fired.set)
        cache.get(self.host, self.port, "abc123")
        self.assertTrue(fired.wait(3), "on_ready never fired")
        self._drain(cache)

    def test_a_failed_fetch_reports_none_and_does_not_crash(self):
        cache = self._cache()
        cache.get(self.host, self.port, "missing")
        self._drain(cache)
        self.assertIsNone(cache.get(self.host, self.port, "missing"))

    def test_prunes_old_files_beyond_the_cap(self):
        cache = self._cache()
        cache._dir = self.tmp.name  # write straight into tmp for easy inspection
        os.makedirs(cache._dir, exist_ok=True)
        for i in range(art.MAX_CACHED + 5):
            with open(os.path.join(cache._dir, "old%d.jpg" % i), "wb") as handle:
                handle.write(b"x")
            # give each file a distinct mtime so pruning order is deterministic
            stamp = time.time() - (art.MAX_CACHED + 5 - i)
            os.utime(os.path.join(cache._dir, "old%d.jpg" % i), (stamp, stamp))
        keep = os.path.join(cache._dir, "keepme.jpg")
        with open(keep, "wb") as handle:
            handle.write(b"x")
        art._prune(cache._dir, keep=keep)
        remaining = os.listdir(cache._dir)
        self.assertIn("keepme.jpg", remaining)
        self.assertLessEqual(len(remaining), art.MAX_CACHED)


class TestCachingSession(ArtServerTestCase):
    class _FakeSession:
        def __init__(self, payload):
            self._payload = payload
            self.commands = []

        def snapshot(self):
            return self._payload

        def command(self, verb, arg=None):
            self.commands.append((verb, arg))

    def test_adds_art_path_beside_image_key(self):
        payload = {
            "v": 1, "status": "ok",
            "core": {"host": self.host, "http_port": self.port, "name": "yavin"},
            "zone": {"id": "z1", "now_playing": {"image_key": "abc123", "art_path": None}},
            "zones": [],
        }
        cache = self._cache()
        wrapped = art.CachingSession(self._FakeSession(payload), cache)
        snap = wrapped.snapshot()
        self.assertIn("art_path", snap["zone"]["now_playing"])
        # Drain the background fetch this kicked off before teardown removes
        # the tempdir, so cleanup never races the fetch thread's file writes.
        thread = cache._last_thread
        if thread is not None:
            thread.join(3)

    def test_leaves_payload_alone_when_there_is_no_zone(self):
        payload = {"v": 1, "status": "connecting", "core": None, "zone": None, "zones": []}
        wrapped = art.CachingSession(self._FakeSession(payload), self._cache())
        self.assertEqual(wrapped.snapshot(), payload)

    def test_leaves_payload_alone_when_nothing_is_playing(self):
        payload = {
            "v": 1, "status": "ok",
            "core": {"host": self.host, "http_port": self.port, "name": "yavin"},
            "zone": {"id": "z1", "now_playing": None},
            "zones": [],
        }
        wrapped = art.CachingSession(self._FakeSession(payload), self._cache())
        snap = wrapped.snapshot()
        self.assertIsNone(snap["zone"]["now_playing"])

    def test_forwards_commands_to_the_wrapped_session(self):
        fake = self._FakeSession({"v": 1, "status": "ok", "core": None, "zone": None, "zones": []})
        wrapped = art.CachingSession(fake, self._cache())
        wrapped.command("seek", 42)
        self.assertEqual(fake.commands, [("seek", 42)])

    def _cache(self):
        return art.Cache(self.tmp.name)


if __name__ == "__main__":
    unittest.main()
