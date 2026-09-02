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
import unittest.mock

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


def png_bytes(width=64, height=64):
    """A PNG signature plus the IHDR the daemon reads dimensions out of."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR"
            + ihdr)


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
        return art.Cache(self.tmp.name)

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
        return art.Cache(self.tmp.name)

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
