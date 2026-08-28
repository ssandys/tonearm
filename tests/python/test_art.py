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

import http.client
import http.server
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

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

    def test_a_truncated_body_reports_failure_rather_than_raising(self):
        # http.client.HTTPException (e.g. IncompleteRead) is a sibling of
        # OSError, not a subclass of it -- an except clause that only names
        # OSError/URLError/ValueError lets it straight through and crashes
        # the fetch thread instead of degrading to art_path: null.
        dest = os.path.join(self.tmp.name, "art", "abc123.jpg")
        truncated = http.client.IncompleteRead(partial=b"", expected=100)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=truncated):
            ok = art.fetch(self.host, self.port, "abc123", dest)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))


class TestImageUrl(unittest.TestCase):
    def test_image_key_is_percent_encoded(self):
        # image_key comes from the Core and is untrusted; it must not be
        # interpolated into the URL raw, mirroring the caution _safe_name
        # already applies on the filesystem side.
        url = art._image_url("host", 9330, "a b/c?d")
        self.assertEqual(
            url, "http://host:9330/api/image/a%20b%2Fc%3Fd?scale=fit&width=64&height=64")

    def test_an_ordinary_key_round_trips_unchanged(self):
        url = art._image_url("192.168.50.118", 9330, "abc123")
        self.assertEqual(
            url,
            "http://192.168.50.118:9330/api/image/abc123?scale=fit&width=64&height=64")


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

    def test_the_art_directory_is_created_eagerly(self):
        # Task 13's unit sets RuntimeDirectory=tonearm, and systemd deletes
        # that whole directory every time the service stops. If art/ were
        # only ever created lazily inside fetch(), a daemon that starts and
        # stays connecting/unpaired/unreachable -- or whose followed zone
        # never has art -- would never create it for that entire run. None
        # of the other TestCache tests catch this: they all go through
        # get()/fetch(), which would create it as a side effect regardless.
        art_dir = os.path.join(self.tmp.name, "art")
        self.assertFalse(os.path.isdir(art_dir))
        self._cache()
        self.assertTrue(os.path.isdir(art_dir))

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


class TestCachingSessionSerialization(unittest.TestCase):
    """Before art.CachingSession existed, RoonSession.snapshot() (and the
    Arbiter mutations inside it) was only ever entered from one thread, the
    Roon event callback. tonearmd's wiring adds two more: a server.py
    handler thread per `subscribe`, and the art cache's on_ready-triggered
    rebroadcast. This proves CachingSession.snapshot() actually serializes
    concurrent callers rather than merely happening to look correct.
    """

    class _CountingSession:
        """Detects overlapping snapshot() calls: tracks how many are
        simultaneously inside the method, and the highest count observed.
        A real overlap would show peak_concurrent > 1; the sleep widens the
        window so a missing lock would reliably be caught, not so the
        result depends on timing -- a correct lock makes peak_concurrent
        == 1 no matter how the sleep or thread count are tuned.
        """

        def __init__(self):
            self._lock = threading.Lock()
            self._active = 0
            self.peak_concurrent = 0

        def snapshot(self):
            with self._lock:
                self._active += 1
                self.peak_concurrent = max(self.peak_concurrent, self._active)
            time.sleep(0.05)
            with self._lock:
                self._active -= 1
            return {"v": 1, "status": "ok", "core": None, "zone": None, "zones": []}

        def command(self, verb, arg=None):
            pass

    def test_concurrent_snapshot_calls_never_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self._CountingSession()
            wrapped = art.CachingSession(session, art.Cache(tmp))

            threads = [threading.Thread(target=wrapped.snapshot) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(3)

            self.assertEqual(
                session.peak_concurrent, 1,
                "two snapshot() calls overlapped -- CachingSession is not "
                "serializing access to the wrapped session")


if __name__ == "__main__":
    unittest.main()
