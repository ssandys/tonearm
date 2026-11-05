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
import re
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import unittest.mock
import zlib

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import art

def jpeg_bytes(width=64, height=64):
    """A JPEG header the daemon can actually parse: SOI, APP0/JFIF, SOF0, EOI.

    The old fixture was `b"\\xff\\xd8\\xff\\xe0fake-jpeg-bytes"` -- the right
    two magic bytes followed by nothing a dimension parser could read. Once
    the daemon validates what it fetched, a fixture that is not an image is
    testing the refusal path, not the success path.
    """
    return (b"\xff\xd8"                                     # SOI
            b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xc0\x00\x11\x08"                          # SOF0, 8-bit
            + struct.pack(">HH", height, width)
            + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
            + b"\xff\xd9")                                   # EOI


def png_chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body)))


def png_bytes(width=64, height=64, before_idat=(), after_idat=()):
    """A complete, well-formed PNG: IHDR, IDAT, IEND, with valid CRCs.

    The daemon now walks every chunk, so a header-only fixture would test
    the refusal path rather than the success path. `before_idat` and
    `after_idat` are (type, body) pairs inserted around the image data. Text
    chunks are legal on either side of it.
    """
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    # RGB, 8-bit, filter byte 0 per row. Only two rows' worth: no decoder
    # runs here, so the IDAT need only be a well-formed chunk.
    idat = zlib.compress(b"\x00" + b"\x00" * 3 * min(width, 64))
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr)
            + b"".join(png_chunk(k, b) for k, b in before_idat)
            + png_chunk(b"IDAT", idat)
            + b"".join(png_chunk(k, b) for k, b in after_idat)
            + png_chunk(b"IEND", b""))


# A zTXt chunk: 16 KiB on the wire that a text-enabled Qt build inflates to
# 16 MiB in the shell process. Repeat it and the caps stay satisfied while
# the expansion does not. This is the shape of the marketplace finding.
ZTXT_BOMB = (b"zTXt", b"Comment\x00\x00" + zlib.compress(b"A" * (16 << 20), 9))


IMAGE_BYTES = jpeg_bytes()


class _ImageHandler(http.server.BaseHTTPRequestHandler):
    """A tiny stand-in for the Core's /api/image/<key> endpoint."""

    def do_GET(self):  # noqa: N802 (stdlib method name)
        if self.path.startswith("/api/image/missing"):
            self.send_response(404)
            self.end_headers()
            return
        if self.path.startswith("/api/image/huge"):
            # A Core that ignores ?width=64&height=64, or anything else
            # answering on that host:port. The URL asks for a thumbnail; only
            # the reader can enforce that it got one.
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(b"\x00" * (art.MAX_ART_BYTES + 4096))
            return
        if self.path.startswith("/api/image/endless"):
            # Sends the cap, then stalls. A reader that stops at the cap is
            # done in milliseconds; one that reads to EOF waits here.
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(b"\x00" * (art.MAX_ART_BYTES + 1))
            self.wfile.flush()
            time.sleep(art.FETCH_TIMEOUT * 2)
            return
        if self.path.startswith("/api/image/offsite"):
            # A Core -- or anything else answering on that host:port -- aiming
            # the daemon's fetch somewhere it never asked to go.
            self.send_response(302)
            self.send_header("Location", self.server.offsite_url)
            self.end_headers()
            return
        if self.path.startswith("/api/image/samesite"):
            self.send_response(302)
            self.send_header("Location", "/api/image/ok")
            self.end_headers()
            return
        if self.path.startswith("/api/image/notanimage"):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html>not an image at all</html>")
            return
        if self.path.startswith("/api/image/bomb"):
            # Small on the wire, enormous once decoded: a few hundred bytes
            # of PNG header declaring 30000x30000, which is 3.6 GB of RGBA
            # in whatever process opens it -- here, the shared shell.
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png_bytes(30000, 30000))
            return
        if self.path.startswith("/api/image/pngtext"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png_bytes(
                before_idat=[ZTXT_BOMB, (b"tEXt", b"Title\x00cover")],
                after_idat=[(b"iTXt", b"Note\x00\x00\x00\x00\x00hi")]))
            return
        if self.path.startswith("/api/image/png"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png_bytes())
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
        # A second origin the daemon must never be talked into contacting.
        # It records every request it receives; the assertion is that the
        # list stays empty.
        self.offsite_hits = []
        offsite_hits = self.offsite_hits

        class _Offsite(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib method name)
                offsite_hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(IMAGE_BYTES)

            def log_message(self, *_args):
                pass

        self.offsite = http.server.HTTPServer(("127.0.0.1", 0), _Offsite)
        threading.Thread(target=self.offsite.serve_forever, daemon=True).start()
        self.addCleanup(self.offsite.shutdown)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _ImageHandler)
        self.httpd.offsite_url = "http://127.0.0.1:%d/api/image/stolen" % (
            self.offsite.server_address[1],)
        self.host, self.port = self.httpd.server_address
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def finish_fetch(self, cache):
        # An unsafe cached file starts a replacement fetch. Join its trailing
        # write/prune before the fixture removes the temporary directory.
        worker = cache._last_thread
        if worker is not None:
            worker.join(art.FETCH_TIMEOUT + 1)
            self.assertFalse(worker.is_alive(), "fetch outlived fixture cleanup")


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
        # Patched on the module's own opener, not urllib.request.urlopen:
        # fetch() goes through _OPENER so that redirects are constrained to
        # the Core's origin, and a mock on urlopen would never be reached.
        with unittest.mock.patch.object(art._OPENER, "open",
                                        side_effect=truncated):
            ok = art.fetch(self.host, self.port, "abc123", dest)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))


class TestImageUrl(unittest.TestCase):
    def test_image_key_is_percent_encoded(self):
        # image_key comes from the Core and is untrusted; it must not be
        # interpolated into the URL raw, mirroring the caution _safe_name
        # already applies on the filesystem side.
        url = art._image_url("host", 9330, "a b/c?d", px=64)
        self.assertEqual(
            url, "http://host:9330/api/image/a%20b%2Fc%3Fd?scale=fit&width=64&height=64")

    def test_an_ordinary_key_round_trips_unchanged(self):
        url = art._image_url("192.168.50.118", 9330, "abc123", px=64)
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

    def test_a_file_that_vanishes_mid_prune_is_skipped_not_raised(self):
        """Two fetches finishing together: one unlinks what the other listed.

        `_prune` guarded `listdir` and `unlink` but not the `getmtime` in the
        sort key between them, so a file removed in that window raised
        FileNotFoundError out of a function documented best-effort -- from
        `_fetch_and_prune`, a thread target, so it surfaced as an unhandled
        traceback and left the cache over its cap.
        """
        cache = self._cache()
        cache._dir = self.tmp.name
        os.makedirs(cache._dir, exist_ok=True)
        for i in range(art.MAX_CACHED + 5):
            with open(os.path.join(cache._dir, "old%d.jpg" % i), "wb") as handle:
                handle.write(b"x")
            stamp = time.time() - (art.MAX_CACHED + 5 - i)
            os.utime(os.path.join(cache._dir, "old%d.jpg" % i), (stamp, stamp))
        keep = os.path.join(cache._dir, "keepme.jpg")
        with open(keep, "wb") as handle:
            handle.write(b"x")

        real_listdir = os.listdir

        def listdir_with_a_ghost(path):
            # Present at listdir time, gone by the time the sort stats it.
            return real_listdir(path) + ["vanished.jpg"]

        with patch.object(art.os, "listdir", listdir_with_a_ghost):
            art._prune(cache._dir, keep=keep)   # must not raise

        remaining = real_listdir(cache._dir)
        self.assertIn("keepme.jpg", remaining)
        # The ghost must not have aborted the prune partway through.
        self.assertLessEqual(len(remaining), art.MAX_CACHED)


class TestCachingSession(ArtServerTestCase):
    class _FakeSession:
        def __init__(self, payload):
            self._payload = payload
            self.commands = []
            self.browse_calls = []

        def snapshot(self):
            return self._payload

        def command(self, verb, arg=None):
            self.commands.append((verb, arg))

        def browse(self, key, op, **kwargs):
            self.browse_calls.append((key, op, kwargs))
            return {"ok": True, "op": op}

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

    def test_forwards_browse_to_the_wrapped_session_with_arguments_intact(self):
        fake = self._FakeSession({"v": 1, "status": "ok", "core": None, "zone": None, "zones": []})
        wrapped = art.CachingSession(fake, self._cache())
        reply = wrapped.browse("widget", "search", term="x")
        self.assertEqual(fake.browse_calls, [("widget", "search", {"term": "x"})])
        self.assertEqual(reply, {"ok": True, "op": "search"})

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


class TestCachingSessionForwardsWhatServerNeeds(unittest.TestCase):
    """The bug this guards against: CachingSession is a decorator that
    silently drops any session method it doesn't explicitly forward, and a
    missing one only surfaces as AttributeError on a live call -- which is
    exactly how `browse` shipped broken, discovered only against a real
    Core. This reads the required method list out of server.py itself
    (every `self._session.<name>(` call site) rather than hardcoding it, so
    a future verb that adds a new self._session.<name>(...) call and
    forgets the matching CachingSession wrapper fails this test immediately
    instead of at runtime.
    """

    def test_exposes_every_session_method_server_py_calls(self):
        server_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "scripts", "tonearm_lib", "server.py")
        with open(server_path, "r") as handle:
            source = handle.read()
        methods = sorted(set(re.findall(r"self\._session\.(\w+)\(", source)))
        # A regex that stopped matching anything would make every assertion
        # below vacuously pass -- guard against that failure mode too.
        self.assertTrue(
            methods,
            "found no self._session.<name>( call sites in server.py -- the "
            "regex is broken, not the code")
        for name in methods:
            self.assertTrue(
                hasattr(art.CachingSession, name),
                "CachingSession has no %r, but server.py calls "
                "self._session.%s(...) -- add a forwarding method" % (name, name))
            self.assertTrue(
                callable(getattr(art.CachingSession, name)),
                "CachingSession.%s exists but is not callable" % name)


# --- hardening: marketplace security review 2026-09-01 -------------------
#
# "Album-art HTTP follows redirects, enabling a LAN Core to redirect requests
# to arbitrary targets; image type/dimension/pixel validation and open-time
# path safety are also missing."


class TestFetchStaysOnTheCore(ArtServerTestCase):
    def test_a_redirect_off_the_core_is_refused(self):
        # urlopen's default handler follows 30x. Whatever answers on the
        # configured host:port could therefore aim the daemon's HTTP client
        # at any other http origin it could reach -- another LAN device, or
        # a service bound to loopback that trusts local callers.
        dest = os.path.join(self.tmp.name, "art.jpg")
        self.assertFalse(art.fetch(self.host, self.port, "offsite", dest))
        self.assertEqual(self.offsite_hits, [],
                         "the daemon was redirected off the Core")
        self.assertFalse(os.path.exists(dest))

    def test_a_redirect_within_the_core_is_still_followed(self):
        # Same origin is not the hazard, and refusing it would break a Core
        # that serves art from a second path.
        dest = os.path.join(self.tmp.name, "art.jpg")
        self.assertTrue(art.fetch(self.host, self.port, "samesite", dest))


class TestFetchValidatesTheImage(ArtServerTestCase):
    def test_a_body_that_is_not_an_image_is_refused(self):
        # The size cap was the ONLY check on the body. An HTML error page,
        # or anything else under 1 MiB, was written to <key>.jpg and handed
        # to ColorQuantizer inside the shared shell process.
        dest = os.path.join(self.tmp.name, "art.jpg")
        self.assertFalse(art.fetch(self.host, self.port, "notanimage", dest))
        self.assertFalse(os.path.exists(dest))

    def test_an_image_declaring_enormous_dimensions_is_refused(self):
        # A decode bomb is small on the wire and huge in memory, so a byte
        # cap cannot see it. 30000x30000 is 3.6 GB of RGBA in the shell.
        dest = os.path.join(self.tmp.name, "art.jpg")
        self.assertFalse(art.fetch(self.host, self.port, "bomb", dest))
        self.assertFalse(os.path.exists(dest))

    def test_a_png_within_the_bounds_is_accepted(self):
        dest = os.path.join(self.tmp.name, "art.png")
        self.assertTrue(art.fetch(self.host, self.port, "png", dest))


class TestImageDimensions(unittest.TestCase):
    def test_reads_jpeg_dimensions_past_a_leading_app0_segment(self):
        self.assertEqual(art.image_dimensions(jpeg_bytes(640, 480)), (640, 480))

    def test_reads_png_dimensions_from_ihdr(self):
        self.assertEqual(art.image_dimensions(png_bytes(320, 200)), (320, 200))

    def test_returns_none_for_bytes_that_are_not_an_image(self):
        self.assertIsNone(art.image_dimensions(b"<!DOCTYPE html>"))

    def test_returns_none_for_a_jpeg_with_no_frame_header(self):
        # The right two magic bytes are not an image. A parser that gave up
        # and said "probably fine" would let exactly this through.
        self.assertIsNone(art.image_dimensions(b"\xff\xd8\xff\xe0truncated"))

    def test_returns_none_rather_than_raising_on_a_truncated_png(self):
        self.assertIsNone(art.image_dimensions(png_bytes()[:12]))

    def test_returns_none_for_zero_dimensions(self):
        self.assertIsNone(art.image_dimensions(png_bytes(0, 0)))


class TestOnlyAnImageIsPublished(ArtServerTestCase):
    def cache(self):
        cache = art.Cache(self.tmp.name)
        self.addCleanup(self.finish_fetch, cache)
        return cache

    def _plant(self, cache, data):
        dest = art.path_for(cache._dir, "k")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(data)
        return dest

    def test_a_file_that_is_not_an_image_is_not_published(self):
        # is_publishable() checked only "regular file, within 1 MiB". The
        # shell will decode whatever is at this path, so the producer-side
        # guard has to know it is an image.
        cache = self.cache()
        self._plant(cache, b"#!/bin/sh\necho not an image\n")
        self.assertIsNone(cache.get(self.host, self.port, "k"))

    def test_a_planted_decode_bomb_is_not_published(self):
        cache = self.cache()
        self._plant(cache, png_bytes(30000, 30000))
        self.assertIsNone(cache.get(self.host, self.port, "k"))

    def test_is_publishable_reports_on_the_descriptor_it_opened(self):
        # The check used os.lstat(path) and then handed the PATH onward. It
        # now opens the file O_NOFOLLOW and answers from that descriptor, so
        # the daemon's own check cannot be aimed at a different file than the
        # one it inspected.
        cache = self.cache()
        dest = self._plant(cache, IMAGE_BYTES)
        self.assertTrue(art.is_publishable(dest))
        os.unlink(dest)
        os.symlink("/etc/passwd", dest)
        self.addCleanup(os.unlink, dest)
        self.assertFalse(art.is_publishable(dest))

    def test_a_directory_at_the_cache_path_is_not_published(self):
        cache = self.cache()
        dest = art.path_for(cache._dir, "k")
        os.makedirs(dest, exist_ok=True)
        self.assertIsNone(cache.get(self.host, self.port, "k"))


class TestPngTextIsNeverWritten(ArtServerTestCase):
    """Compressed PNG chunks must not reach the shell's decoder.

    Marketplace review of 0.14.x: fetch() checked the byte size and the IHDR
    dimensions and then wrote the PNG unchanged. Qt's PNG reader inflates
    zTXt/iTXt into QImage::text(), so text chunks within both caps could
    still exhaust the shared shell's memory.
    """

    def _chunk_types(self, data):
        types, i = [], 8
        while i < len(data):
            (length,) = struct.unpack(">I", data[i:i + 4])
            types.append(data[i + 4:i + 8])
            i += 12 + length
        return types

    def test_fetch_writes_the_png_without_its_text_chunks(self):
        dest = os.path.join(self.tmp.name, "art.png")
        self.assertTrue(art.fetch(self.host, self.port, "pngtext", dest))
        with open(dest, "rb") as handle:
            written = handle.read()
        self.assertEqual(self._chunk_types(written), [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(written, png_bytes())
        self.assertTrue(art.is_publishable(dest))

    def test_sanitize_keeps_the_chunks_needed_to_draw_in_order(self):
        data = png_bytes(before_idat=[(b"gAMA", b"\x00\x00\xb1\x8f"),
                                      (b"PLTE", b"\x00" * 3),
                                      (b"iCCP", b"p\x00\x00" + zlib.compress(b"x")),
                                      (b"tRNS", b"\x00"),
                                      (b"eXIf", b"MM\x00*")])
        self.assertEqual(self._chunk_types(art.sanitize_png(data)),
                         [b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"])

    def test_sanitize_refuses_rather_than_repairs(self):
        good = png_bytes()
        bad_crc = good[:-1] + bytes([good[-1] ^ 1])
        no_iend = good[:-12]
        no_idat = (b"\x89PNG\r\n\x1a\n" + good[8:33]
                   + png_chunk(b"IEND", b""))
        not_ihdr_first = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"tEXt", b"a\x00b")
                          + good[8:])
        overrun = good[:33] + struct.pack(">I", 1 << 30) + good[37:]
        for name, data in [("bad crc", bad_crc), ("no IEND", no_iend),
                           ("no IDAT", no_idat),
                           ("IHDR not first", not_ihdr_first),
                           ("length past the end", overrun)]:
            with self.subTest(name):
                self.assertIsNone(art.sanitize_png(data))

    def test_trailing_bytes_after_iend_are_dropped(self):
        self.assertEqual(art.sanitize_png(png_bytes() + b"junk"), png_bytes())

    def test_a_planted_png_with_text_is_not_published(self):
        # The fetch path strips it; is_publishable must also refuse a file
        # swapped in afterwards. The text sits AFTER the image data and past
        # the first 64 KiB, where a header-only re-check never looked.
        dest = os.path.join(self.tmp.name, "planted.png")
        padding = [(b"tEXt", b"pad\x00" + b"p" * (70 * 1024))]
        with open(dest, "wb") as handle:
            handle.write(png_bytes(after_idat=padding + [ZTXT_BOMB]))
        self.assertLess(os.path.getsize(dest), art.MAX_ART_BYTES)
        self.assertFalse(art.is_publishable(dest))

    def test_a_clean_png_and_a_jpeg_are_still_published(self):
        for name, data in [("png", png_bytes()), ("jpeg", IMAGE_BYTES)]:
            with self.subTest(name):
                dest = os.path.join(self.tmp.name, "ok." + name)
                with open(dest, "wb") as handle:
                    handle.write(data)
                self.assertTrue(art.is_publishable(dest))


if __name__ == "__main__":
    unittest.main()


class TestFetchIsBounded(ArtServerTestCase):
    """The daemon must not read an unbounded body, or write through a symlink.

    Both patterns were rejected by name in the marketplace review of a sibling
    plugin (HANCORE-linux/omarchy-plugin-marketplace#2659): unbounded
    consumption on one round, and "predictable `headway.json.tmp` with direct
    redirection, so a planted symlink can redirect the write" on the next.
    This module had the same two shapes.
    """

    def test_the_read_STOPS_at_the_cap_rather_than_buffering_the_body(self):
        """The bound is on the read, not merely on a check afterwards.

        Refusing an oversize body after reading all of it still lets whatever
        answers on that host:port decide how much memory the daemon spends and
        how long it spends there. Only elapsed time can tell the two apart:
        this endpoint sends exactly the cap and then stalls, so a reader that
        stops at the cap returns immediately while one that reads to EOF waits
        for the stall (or for FETCH_TIMEOUT to fire, which is just as slow).
        """
        dest = os.path.join(self.tmp.name, "endless.jpg")
        started = time.monotonic()
        result = art.fetch(self.host, self.port, "endless", dest)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, art.FETCH_TIMEOUT / 2,
                        "read took %.1fs; it should stop at the cap" % elapsed)
        self.assertFalse(result)
        self.assertFalse(os.path.exists(dest))

    def test_an_oversize_body_is_refused_and_writes_nothing(self):
        dest = os.path.join(self.tmp.name, "huge.jpg")
        self.assertFalse(art.fetch(self.host, self.port, "huge", dest))
        self.assertFalse(os.path.exists(dest))

    def test_a_symlink_planted_at_the_old_temp_path_cannot_redirect_the_write(self):
        # `dest + ".tmp"` was the temp name, opened with a plain "wb" -- which
        # follows a symlink and truncates whatever it points at.
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as handle:
            handle.write("untouched")
        dest = os.path.join(self.tmp.name, "art.jpg")
        os.symlink(victim, dest + ".tmp")

        self.assertTrue(art.fetch(self.host, self.port, "k", dest))
        with open(victim) as handle:
            self.assertEqual(handle.read(), "untouched")

    def test_no_temp_file_survives_a_successful_fetch(self):
        dest = os.path.join(self.tmp.name, "art.jpg")
        self.assertTrue(art.fetch(self.host, self.port, "k", dest))
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_no_temp_file_survives_a_refused_fetch(self):
        # _prune deliberately skips *.tmp, so an orphan would never be cleaned
        # up by anything else.
        dest = os.path.join(self.tmp.name, "huge.jpg")
        art.fetch(self.host, self.port, "huge", dest)
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestOnlyASafeFileIsPublished(ArtServerTestCase):
    """`art_path` is consumed by ColorQuantizer inside the shared shell process.

    Quickshell exports no filesystem primitive and ColorQuantizer carries no
    size cap, no stat and no symlink control -- the same gap `FileView` has,
    which is what the sibling plugin's first finding was about. The bound has
    to be enforced producer-side, before the path reaches QML, which is
    exactly the remedy that review asked for.
    """

    def cache(self):
        cache = art.Cache(self.tmp.name)
        self.addCleanup(self.finish_fetch, cache)
        return cache

    def test_a_real_cached_file_is_published(self):
        cache = self.cache()
        dest = art.path_for(cache._dir, "k")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(IMAGE_BYTES)
        self.assertEqual(cache.get(self.host, self.port, "k"), dest)

    def test_a_symlink_at_the_cache_path_is_not_published(self):
        # os.path.exists() follows symlinks, so the old check published one.
        cache = self.cache()
        dest = art.path_for(cache._dir, "k")
        victim = os.path.join(self.tmp.name, "secret")
        with open(victim, "wb") as handle:
            handle.write(b"not an image")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.symlink(victim, dest)
        self.assertIsNone(cache.get(self.host, self.port, "k"))

    def test_an_oversize_file_at_the_cache_path_is_not_published(self):
        cache = self.cache()
        dest = art.path_for(cache._dir, "k")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(b"\x00" * (art.MAX_ART_BYTES + 1))
        self.assertIsNone(cache.get(self.host, self.port, "k"))

    def test_a_non_regular_file_is_not_published(self):
        # A FIFO would stall whatever opened it -- here, the shared shell.
        cache = self.cache()
        dest = art.path_for(cache._dir, "k")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.mkfifo(dest)
        self.addCleanup(os.unlink, dest)
        self.assertIsNone(cache.get(self.host, self.port, "k"))


# --- marketplace security review 2026-10-31 --------------------------------
#
# "Panel.qml:406-416 loads Model.artUrl() directly in Qt Image; Model.js
# constructs the configured Roon Core's plaintext HTTP image URL. An attacker
# controlling/spoofing that referenced Core response can exhaust shared-shell
# memory with an oversized body before decoding; sourceSize acts later, and
# the separate art helper's byte cap/tonearmd MemoryMax do not contain this
# shell-side fetch."
#
# The widget no longer fetches anything. Every cover it shows -- now playing
# and every browse row -- is fetched here, through fetch()'s caps, and reaches
# QML only as a path in the 0700 cache.


class TestNowPlayingArtIsDisplaySized(ArtServerTestCase):
    """The cached copy is now what the popup DISPLAYS, not only what
    ColorQuantizer samples, so it is fetched at display size."""

    def test_the_now_playing_copy_is_fetched_at_display_size(self):
        cache = art.Cache(self.tmp.name)
        with patch.object(art, "fetch", wraps=art.fetch) as spy:
            cache.get(self.host, self.port, "abc123")
            cache._last_thread.join(3)
        self.assertTrue(spy.called)
        self.assertEqual(spy.call_args.kwargs.get("px"), art.NOW_PLAYING_PX)

    def test_display_size_is_sharp_and_still_inside_the_bounds(self):
        # The popup draws the cover at roughly 236 device pixels by default
        # and more under a scaled theme; 64 was a quantizer sample, not art.
        self.assertGreaterEqual(art.NOW_PLAYING_PX, 256)
        self.assertLessEqual(art.NOW_PLAYING_PX, art.MAX_ART_DIMENSION)
        self.assertLessEqual(art.ROW_PX, art.MAX_ART_DIMENSION)

    def test_the_url_asks_for_the_requested_size(self):
        url = art._image_url("h", 9330, "k", px=art.ROW_PX)
        self.assertTrue(url.endswith("width=%d&height=%d" % (art.ROW_PX, art.ROW_PX)), url)

    def test_fetch_honours_a_smaller_byte_cap(self):
        dest = os.path.join(self.tmp.name, "rowart", "k.jpg")
        self.assertFalse(art.fetch(self.host, self.port, "k", dest,
                                   max_bytes=len(IMAGE_BYTES) - 1))
        self.assertFalse(os.path.exists(dest))
        self.assertTrue(art.fetch(self.host, self.port, "k", dest,
                                  max_bytes=len(IMAGE_BYTES)))

    def test_is_publishable_honours_a_smaller_byte_cap(self):
        dest = os.path.join(self.tmp.name, "k.jpg")
        with open(dest, "wb") as handle:
            handle.write(IMAGE_BYTES)
        self.assertTrue(art.is_publishable(dest, max_bytes=len(IMAGE_BYTES)))
        self.assertFalse(art.is_publishable(dest, max_bytes=len(IMAGE_BYTES) - 1))


class TestRowArtCache(ArtServerTestCase):
    def cache(self):
        rows = art.RowArtCache(self.tmp.name)
        self.addCleanup(rows.close)
        return rows

    def test_its_directory_is_created_eagerly_beside_the_now_playing_one(self):
        rows = self.cache()
        self.assertEqual(rows._dir, os.path.join(self.tmp.name, "rowart"))
        self.assertTrue(os.path.isdir(rows._dir))
        self.assertEqual(stat_mode(rows._dir), 0o700)

    def test_first_get_schedules_and_a_later_one_returns_the_path(self):
        rows = self.cache()
        self.assertIsNone(rows.get(self.host, self.port, "abc123"))
        rows.wait(["abc123"], 3)
        path = rows.get(self.host, self.port, "abc123")
        self.assertEqual(path, art.path_for(rows._dir, "abc123"))
        self.assertTrue(art.is_publishable(path, max_bytes=art.MAX_ROW_ART_BYTES))

    def test_rows_are_fetched_at_row_size_under_the_row_byte_cap(self):
        rows = self.cache()
        with patch.object(art, "fetch", wraps=art.fetch) as spy:
            rows.get(self.host, self.port, "abc123")
            rows.wait(["abc123"], 3)
        self.assertEqual(spy.call_args.kwargs.get("px"), art.ROW_PX)
        self.assertEqual(spy.call_args.kwargs.get("max_bytes"), art.MAX_ROW_ART_BYTES)
        self.assertLess(art.MAX_ROW_ART_BYTES, art.MAX_ART_BYTES)

    def test_a_failed_key_is_not_refetched_on_every_browse(self):
        rows = self.cache()
        with patch.object(art, "fetch", wraps=art.fetch) as spy:
            for _ in range(3):
                self.assertIsNone(rows.get(self.host, self.port, "missing"))
                rows.wait(["missing"], 3)
        self.assertEqual(spy.call_count, 1)

    def test_a_key_already_queued_is_not_queued_twice(self):
        rows = self.cache()
        gate = threading.Event()
        calls = []

        def slow(*args, **kwargs):
            calls.append(args[2])
            gate.wait(3)
            return False

        with patch.object(art, "fetch", side_effect=slow):
            rows.get(self.host, self.port, "k")
            rows.get(self.host, self.port, "k")
            gate.set()
            rows.wait(["k"], 3)
        self.assertEqual(calls, ["k"])

    def test_the_queue_is_bounded(self):
        rows = self.cache()
        gate = threading.Event()
        self.addCleanup(gate.set)

        def blocked(*_args, **_kwargs):
            gate.wait(3)
            return False

        with patch.object(art, "MAX_ROW_PENDING", 3), \
                patch.object(art, "fetch", side_effect=blocked):
            for i in range(20):
                rows.get(self.host, self.port, "k%d" % i)
            self.assertLessEqual(len(rows._pending), 3)
            gate.set()

    def test_no_host_or_key_schedules_nothing(self):
        rows = self.cache()
        self.assertIsNone(rows.get(None, self.port, "k"))
        self.assertIsNone(rows.get(self.host, self.port, None))
        self.assertEqual(rows._pending, {})

    def test_wait_returns_promptly_for_unknown_keys(self):
        rows = self.cache()
        started = time.monotonic()
        rows.wait(["never-scheduled"], 5)
        self.assertLess(time.monotonic() - started, 1)


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


class TestCachingSessionAttachesRowArt(ArtServerTestCase):
    class _Browsing:
        def __init__(self, payload, rows):
            self._payload = payload
            self.rows = rows

        def snapshot(self):
            return self._payload

        def command(self, verb, arg=None):
            pass

        def browse(self, key, op, **kwargs):
            # Shallow copy, the way BrowseSession.current() returns its rows:
            # the row DICTS are shared with the session.
            return {"ok": True, "level_id": 3, "path": ["Search"],
                    "rows": list(self.rows)}

    def setUp(self):
        super().setUp()
        self.payload = {
            "v": 1, "status": "ok",
            "core": {"host": self.host, "http_port": self.port, "name": "yavin"},
            "zone": None, "zones": [],
        }
        self.rows = [
            {"title": "Oingo Boingo", "image_key": "artist1"},
            {"title": "Albums", "image_key": None},
            {"title": "Dead Man's Party", "image_key": "album1"},
        ]
        self.cache = art.Cache(self.tmp.name)
        self.row_cache = art.RowArtCache(self.tmp.name)
        self.addCleanup(self.row_cache.close)
        self.inner = self._Browsing(self.payload, self.rows)
        self.session = art.CachingSession(self.inner, self.cache, self.row_cache)

    def test_rows_carry_a_local_art_path_and_never_a_url(self):
        self.session.snapshot()
        reply = self.session.browse("widget", "search", term="oingo")
        paths = [r["art_path"] for r in reply["rows"]]
        self.assertEqual(paths, [art.path_for(self.row_cache._dir, "artist1"),
                                 None,
                                 art.path_for(self.row_cache._dir, "album1")])
        self.assertNotIn("http", repr(reply))

    def test_the_wrapped_session_s_rows_are_not_mutated(self):
        self.session.snapshot()
        self.session.browse("widget", "search", term="oingo")
        self.assertNotIn("art_path", self.rows[0])

    def test_level_fields_pass_through_unchanged(self):
        self.session.snapshot()
        reply = self.session.browse("widget", "search", term="oingo")
        self.assertEqual((reply["ok"], reply["level_id"], reply["path"]),
                         (True, 3, ["Search"]))

    def test_rows_past_the_wait_window_arrive_on_a_later_current(self):
        many = [{"title": "r%d" % i, "image_key": "key%d" % i} for i in range(30)]
        self.inner.rows = many
        self.session.snapshot()
        with patch.object(art, "ROW_ART_WAIT_ROWS", 2):
            self.session.browse("widget", "search", term="x")
        self.row_cache.wait(["key%d" % i for i in range(30)], 5)
        reply = self.session.browse("widget", "current")
        self.assertTrue(all(r["art_path"] for r in reply["rows"]))

    def test_without_a_known_core_nothing_is_fetched(self):
        with patch.object(art, "fetch") as spy:
            reply = self.session.browse("widget", "search", term="oingo")
        spy.assert_not_called()
        self.assertEqual([r.get("art_path") for r in reply["rows"]], [None] * 3)

    def test_a_disconnected_snapshot_forgets_the_core(self):
        self.session.snapshot()
        self.payload["core"] = None
        self.session.snapshot()
        with patch.object(art, "fetch") as spy:
            self.session.browse("widget", "search", term="oingo")
        spy.assert_not_called()

    def test_a_reply_without_rows_is_returned_as_is(self):
        self.session.snapshot()
        self.inner.browse = lambda *a, **k: {"ok": True, "played": True}
        self.assertEqual(self.session.browse("widget", "activate", index=0),
                         {"ok": True, "played": True})
