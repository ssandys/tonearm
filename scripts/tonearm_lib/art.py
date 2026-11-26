"""Local cache of cover art: the ONLY way art reaches the widget.

Every cover the popup shows -- the now-playing card and every browse row --
is fetched here and handed to QML as a path in the 0700 runtime cache. The
widget never builds a Core URL and never fetches anything itself.

Until 0.14.2 the popup's `Image` elements loaded the Core's
`http://<core>:9330/api/image/<key>` URL directly, from inside omarchy-shell,
the process every bar widget shares. Qt reads that whole response before
decoding it -- `sourceSize` only bounds the decode -- so whatever answered on
that host:port could exhaust shell memory with an oversized body, and none of
the caps below applied. Raised by the Omarchy marketplace security review on
2026-10-31. Routing every fetch through `fetch()` puts the byte cap, the
header-checked dimension cap and the same-origin redirect rule in front of
every image the shell decodes.

Two caches, the same fetcher:

- `Cache` keeps the followed zone's cover at display size in `art/`. The
  popup draws it and `ColorQuantizer` samples it -- which needed a local copy
  regardless: measured in a live Quickshell probe, the quantizer pointed at
  the remote URL emits zero colors, and eight for the same bytes as a file.
- `RowArtCache` keeps browse-row thumbnails in `rowart/`, fetched by a small
  fixed pool so a 100-row page never means 100 threads.

This module owns I/O only. It never decides how art is displayed -- it
emits a path, or `None`, and nothing else. Fetching happens on a background
thread so it can never block a state broadcast; a failed fetch is logged
and reported as `None`, never raised.
"""

from __future__ import annotations

import collections
import http.client
import logging
import os
import queue
import re
import secrets
import stat
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

LOG = logging.getLogger("tonearmd.art")

# Seconds to wait for the Core to answer one image request. This is a small
# image on the LAN; a Core that cannot answer this quickly is not one
# whose art is worth waiting on -- give up and try again next track.
FETCH_TIMEOUT = 5.0

# Requested sizes. The now-playing copy is what the popup DISPLAYS -- about
# 236 device pixels at the default theme scale, more under a larger one -- and
# also what ColorQuantizer samples, which rescales to 64 itself. A browse row
# is drawn at ~60. Both are requests the Core may ignore; the caps below are
# what holds when it does.
NOW_PLAYING_PX = 512
ROW_PX = 128

# How many now-playing images to keep on disk. One image_key per track and only
# the followed zone is ever cached, so this is generous headroom, not a
# tight budget -- just enough to stop the directory from growing without
# bound over a long-running daemon.
MAX_CACHED = 10

# The URL asks the Core for a NOW_PLAYING_PX square, which measures tens of KB.
# Only the reader can enforce that it got one: the request is a preference, and
# whatever answers on that host:port decides what to send. 1 MiB is ~10x the
# real size and still far too small to matter to the daemon.
#
# The same number bounds what is PUBLISHED as `art_path`, because that file is
# read by the popup's Image and ColorQuantizer inside the shared shell
# process, and Quickshell exports no filesystem primitive able to bound it
# there.
MAX_ART_BYTES = 1024 * 1024

# Browse rows: up to a page (browse.PAGE, 100) of them at once, so both the
# per-file cap and the file count are tighter than the now-playing cache's.
# Worst case on disk is MAX_ROW_CACHED * MAX_ROW_ART_BYTES = 40 MiB of tmpfs; a
# real 128px thumbnail is a few KB.
MAX_ROW_ART_BYTES = 256 * 1024
MAX_ROW_CACHED = 160

# Fetch workers for row art, and how many keys may be queued for them. A
# search that returns a page queues at most a page; this bounds what a burst
# of searches -- or a local client looping on browse -- can pile up.
ROW_FETCH_WORKERS = 4
MAX_ROW_PENDING = 200

# A browse reply waits at most ROW_ART_WAIT seconds for the first
# ROW_ART_WAIT_ROWS rows' art -- what the pane shows without scrolling -- so
# the common case renders with art on the first paint. Everything else
# arrives through the widget's `current` refresh.
ROW_ART_WAIT = 1.0
ROW_ART_WAIT_ROWS = 10

# A key whose fetch failed is not retried for this long, so a Core with no
# art for a row is not asked again on every keystroke of navigation.
ROW_RETRY_AFTER = 60.0
MAX_ROW_FAILED = 512

# Bounds on what the bytes DECODE to, which the byte cap above cannot see. A
# few hundred bytes of PNG header can declare 30000x30000 -- 3.6 GB of RGBA in
# whatever opens it, which here is the popup inside the shared shell process.
# The URL asks for at most NOW_PLAYING_PX; 2048 per side is a wide margin for a Core
# that ignores the hint and returns a full-size cover, and still two orders of
# magnitude below a bomb.
MAX_ART_DIMENSION = 2048
MAX_ART_PIXELS = MAX_ART_DIMENSION * MAX_ART_DIMENSION

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")

# Start-of-frame markers, which carry the dimensions. C4 (Huffman table), C8
# (JPEG extensions) and CC (arithmetic coding conditioning) share the range
# but are not frames.
_SOF_MARKERS = frozenset(m for m in range(0xC0, 0xD0)
                         if m not in (0xC4, 0xC8, 0xCC))
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# The only PNG chunks a cover needs in order to be drawn. Everything else is
# dropped before the file is written, above all the ones a decoder INFLATES:
# zTXt and iTXt (compressed text) and iCCP (a compressed ICC profile). Qt's
# PNG reader decompresses text chunks into QImage::text() as it loads, so a
# few kilobytes of zTXt inside the byte and pixel caps could still expand to
# gigabytes in the shared shell process. Dimension checks cannot see that;
# keeping only these chunks removes it. Dropping gAMA/cHRM/sRGB, EXIF and
# animation chunks costs nothing for a thumbnail-sized cover.
_PNG_KEEP = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"})


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]] | None:
    """Split a PNG into (type, chunk bytes) pairs, or None if it is malformed.

    Header walking only. The CRC is checked with zlib.crc32, but nothing is
    ever decompressed. The walk demands IHDR first and ends at IEND. Bytes
    after IEND are discarded, since the caller rebuilds the file anyway. A
    file with no IEND, a bad CRC, a chunk that runs past the end, or a type
    that is not four ASCII letters is refused rather than repaired.
    """
    if not data.startswith(_PNG_MAGIC):
        return None
    chunks = []
    i, end = len(_PNG_MAGIC), len(data)
    while i + 12 <= end:
        (length,) = struct.unpack(">I", data[i:i + 4])
        kind = data[i + 4:i + 8]
        stop = i + 12 + length
        if stop > end or not kind.isalpha() or not kind.isascii():
            return None
        (crc,) = struct.unpack(">I", data[stop - 4:stop])
        if zlib.crc32(data[i + 4:stop - 4]) != crc:
            return None
        if not chunks and kind != b"IHDR":
            return None
        chunks.append((kind, data[i:stop]))
        if kind == b"IEND":
            return chunks
        i = stop
    return None


def sanitize_png(data: bytes) -> bytes | None:
    """`data` rebuilt from the chunks in _PNG_KEEP, or None if malformed.

    The rebuilt file is what gets written to disk, so the shell's decoder
    never sees a text or profile chunk that the Core sent.
    """
    chunks = _png_chunks(data)
    if chunks is None or not any(kind == b"IDAT" for kind, _ in chunks):
        return None
    return _PNG_MAGIC + b"".join(raw for kind, raw in chunks
                                 if kind in _PNG_KEEP)


def png_is_clean(data: bytes) -> bool:
    """Is `data` a well-formed PNG made only of _PNG_KEEP chunks?"""
    chunks = _png_chunks(data)
    return (chunks is not None
            and sum(len(raw) for _, raw in chunks) + len(_PNG_MAGIC)
            == len(data)
            and all(kind in _PNG_KEEP for kind, _ in chunks))


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) if `data` opens with a PNG or JPEG header, else None.

    Header parsing only -- nothing here decodes pixels, which is the whole
    point: the daemon must decide whether the bytes are worth handing to a
    decoder without running one. None means "not an image this daemon will
    publish", which covers a wrong magic number, a truncated header, and a
    frame header that never arrives. There is deliberately no "looks close
    enough" branch: the two JPEG magic bytes followed by junk is exactly the
    shape a refusal has to catch.
    """
    if data.startswith(_PNG_MAGIC):
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    if data.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(data)
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments to the first start-of-frame and read its size."""
    i, end = 2, len(data)
    while i + 3 < end:
        if data[i] != 0xFF:
            return None                     # not at a marker; refuse
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1                          # fill byte
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2                          # standalone: no length follows
            continue
        if marker == 0xDA:
            return None                     # entropy data; no frame header
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if length < 2:
            return None
        if marker in _SOF_MARKERS:
            if i + 9 > end:
                return None
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return (width, height) if width and height else None
        i += 2 + length
    return None


def _within_bounds(dims: tuple[int, int]) -> bool:
    width, height = dims
    if width > MAX_ART_DIMENSION or height > MAX_ART_DIMENSION:
        return False
    return width * height <= MAX_ART_PIXELS


def _origin(url: str) -> tuple:
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme, parts.hostname, parts.port)


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it stays on the Core.

    urlopen's default handler follows any 30x to http, https or ftp. Since
    whatever answers on the configured host:port decides the response, that
    handed it the daemon's HTTP client as a proxy: one Location header and
    the fetch goes to another LAN device, or to a service bound to loopback
    that trusts local callers. Returning None leaves the redirect unhandled,
    which raises HTTPError -- already on fetch()'s refusal path.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            LOG.warning("refusing art redirect off the Core to %s",
                        urllib.parse.urlsplit(newurl).netloc)
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameOriginRedirect)


def _safe_name(image_key: str) -> str:
    # image_key comes from the Core; treat it as untrusted before it becomes
    # part of a filesystem path.
    return _SAFE_KEY.sub("_", image_key) + ".jpg"


def path_for(cache_dir: str, image_key: str) -> str:
    return os.path.join(cache_dir, _safe_name(image_key))


def _image_url(host: str, http_port: int, image_key: str,
               px: int = NOW_PLAYING_PX) -> str:
    # image_key is untrusted (it comes from the Core); percent-encode it
    # rather than interpolating it raw, the same caution _safe_name already
    # applies on the filesystem side.
    size = int(px)
    return "http://%s:%d/api/image/%s?scale=fit&width=%d&height=%d" % (
        host, http_port, urllib.parse.quote(image_key, safe=""), size, size)


def fetch(host: str, http_port: int, image_key: str, dest: str,
          px: int = NOW_PLAYING_PX, max_bytes: int = MAX_ART_BYTES) -> bool:
    """Fetch a `px`-square copy of `image_key`'s art into `dest`.

    Returns whether it succeeded. Never raises: a network error, a timeout,
    a non-2xx response, a redirect off the Core, a malformed/truncated body
    or a body that is not a small image all end in a logged warning and
    `False`, so a bad fetch degrades to `art_path: null` instead of taking
    the daemon down.
    """
    url = _image_url(host, http_port, image_key, px)
    directory = os.path.dirname(dest)
    name = os.path.basename(dest)
    dir_fd = None
    tmp = None
    placed = False
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # Opened once and written through, the same contract config.py uses:
        # every create and the rename below resolve against THIS descriptor,
        # so the walk happens once and cannot resolve differently the second
        # time. O_NOFOLLOW refuses a symlinked art directory outright.
        dir_fd = os.open(directory,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

        # _OPENER, not urlopen: its redirect handler refuses to leave the
        # Core's origin. See _SameOriginRedirect.
        with _OPENER.open(url, timeout=FETCH_TIMEOUT) as response:
            # One byte past the cap, so "at the limit" and "over it" are
            # distinguishable without reading the rest of an endless body.
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            LOG.warning("art for %s exceeds %d bytes; refusing it",
                        image_key, max_bytes)
            return False

        # The byte cap says nothing about what the bytes ARE. Whatever
        # answers on that host:port chose this body, and the file written
        # below is decoded by ColorQuantizer inside the shared shell
        # process -- so an HTML error page, a shell script, or a header
        # declaring 30000x30000 must be refused here, before it is written.
        dims = image_dimensions(data)
        if dims is None:
            LOG.warning("art for %s is not a PNG or JPEG; refusing it",
                        image_key)
            return False
        if not _within_bounds(dims):
            LOG.warning("art for %s declares %dx%d, past the %d limit; "
                        "refusing it", image_key, dims[0], dims[1],
                        MAX_ART_DIMENSION)
            return False
        # Within bounds is not yet safe for a PNG: its ancillary chunks are
        # decoded too, and some of them are compressed. See _PNG_KEEP.
        if data.startswith(_PNG_MAGIC):
            data = sanitize_png(data)
            if data is None:
                LOG.warning("art for %s is a malformed PNG; refusing it",
                            image_key)
                return False

        # An unguessable name created O_EXCL|O_NOFOLLOW. The predictable
        # `dest + ".tmp"` this replaced was openable with a plain "wb",
        # which follows a symlink -- so anything able to plant one at that
        # path redirected the daemon's write to its target. Same directory
        # as `dest`, because os.replace is only atomic within one filesystem.
        tmp = ".art-%s.tmp" % secrets.token_hex(8)
        handle_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                            | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
        # Atomic: a concurrent reader never sees a partial file.
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        placed = True
        return True
    except (OSError, urllib.error.URLError, http.client.HTTPException,
            ValueError):
        # OSError/URLError cover network failures, non-2xx responses and the
        # HTTPError a refused redirect raises; http.client.HTTPException
        # (e.g. IncompleteRead on a truncated body) is a *sibling* of
        # OSError, not a subclass of it, so it needs naming explicitly or it
        # would slip past this handler and crash the fetch thread.
        LOG.warning("art fetch failed for %s", image_key, exc_info=True)
        return False
    finally:
        # _prune deliberately skips *.tmp, so nothing else would ever collect
        # an orphan left by a failed fetch.
        if tmp is not None and not placed and dir_fd is not None:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def is_publishable(path: str, max_bytes: int = MAX_ART_BYTES) -> bool:
    """May this path be handed to the widget as `art_path`?

    Whatever this returns is read by `ColorQuantizer` inside the shared
    omarchy-shell process, which carries no size cap, no stat and no symlink
    control -- the same gap `FileView` has. Quickshell exports no filesystem
    primitive that could bound it on that side, so the bound is enforced here,
    before the path reaches QML.

    Answered from a DESCRIPTOR, not from the path: the file is opened
    `O_NOFOLLOW` and every question -- regular file, size, image header -- is
    asked of that one open file. The `os.lstat(path)` this replaced described
    a file the process never opened, so nothing tied the thing inspected to
    the thing that existed a moment later. `O_NONBLOCK` so a FIFO planted here
    fails the S_ISREG check instead of blocking this open until someone writes
    to it.

    The whole file is re-read rather than trusted from fetch time, because the
    threat is a file swapped after it was written. That costs at most
    `max_bytes` from the page cache. A PNG must also still contain only the
    chunks fetch() keeps, and a text chunk can sit anywhere in the file, even
    after the image data, so checking the header alone is not enough.

    A residual remains and is worth naming rather than papering over: the
    SHELL opens this path afterwards, by name, so a same-user race can still
    swap the file in between. Closing that would need an open-time guarantee
    on the reader's side, and the reader is Qt. The cache directory is 0700,
    so the race needs an actor who is already the user.
    """
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return False
        if info.st_size > max_bytes:
            return False
        with os.fdopen(fd, "rb") as handle:
            fd = None                       # the file object owns it now
            # One byte past the cap: a file that grew after the fstat above
            # is refused, not truncated into something that looks clean.
            data = handle.read(max_bytes + 1)
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(data) > max_bytes:
        return False
    dims = image_dimensions(data)
    if dims is None or not _within_bounds(dims):
        return False
    return not data.startswith(_PNG_MAGIC) or png_is_clean(data)


def _prune(directory: str, keep: str, max_files: int = MAX_CACHED) -> None:
    """Delete the oldest cached files once there are more than `max_files`,
    always keeping `keep`. Best-effort: a directory that cannot be listed or
    a file that cannot be removed is skipped, not raised.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return
    keep_name = os.path.basename(keep)
    # Stat every candidate BEFORE deciding anything, and tolerate a missing
    # one. A name listed a moment ago can already be gone: two fetches
    # finishing together means one prunes what the other listed. The stat
    # used to sit unguarded in the sort key, so that window raised
    # FileNotFoundError out of a function documented best-effort -- and out
    # of `_fetch_and_prune`, a thread target, which made it an unhandled
    # traceback and left the cache over its cap. A file that vanished needs
    # no deleting, which is the outcome wanted anyway.
    dated = []
    for name in names:
        if name == keep_name or name.endswith(".tmp"):
            continue
        try:
            dated.append((os.path.getmtime(os.path.join(directory, name)), name))
        except OSError:
            continue
    over = len(dated) - (max_files - 1)
    if over <= 0:
        return
    # (mtime, name): ties break by name, so the choice is deterministic
    # rather than dependent on directory order.
    dated.sort()
    for _, name in dated[:over]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


class Cache:
    """Tracks the currently-cached `image_key` and fetches a new one in the
    background when it changes.

    `get()` never blocks on network I/O: it returns the cached path
    immediately if the file already exists, kicks off a background fetch
    (at most one in flight per new `image_key`) if it does not, and
    otherwise returns `None`. Callers see `art_path: null` until the fetch
    completes -- there is nothing to await here on purpose.
    """

    def __init__(self, runtime_dir: str, on_ready=None) -> None:
        self._dir = os.path.join(runtime_dir, "art")
        self._on_ready = on_ready
        self._lock = threading.Lock()
        self._last_key: str | None = None
        # Created eagerly, not lazily inside fetch(): Task 13's unit sets
        # RuntimeDirectory=tonearm, and systemd removes that whole directory
        # every time the service stops. A daemon that starts and stays
        # connecting/unpaired/unreachable for its entire run -- or whose
        # followed zone never has art -- would otherwise never create art/
        # at all, since fetch() is the only other place that calls makedirs.
        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        # Set only so tests can join() a fetch deterministically instead of
        # polling for the destination file, which proves the file landed but
        # not that this thread's trailing _prune() has also finished poking
        # the same directory tree -- see test_art.py.
        self._last_thread: threading.Thread | None = None

    def get(self, host: str | None, http_port: int | None,
            image_key: str | None) -> str | None:
        if not image_key or not host:
            return None
        dest = path_for(self._dir, image_key)
        if is_publishable(dest):
            return dest
        with self._lock:
            if self._last_key == image_key:
                return None  # a fetch for this key is already in flight (or failed)
            self._last_key = image_key
        thread = threading.Thread(
            target=self._fetch_and_prune,
            args=(host, http_port or 9330, image_key, dest),
            daemon=True,
        )
        self._last_thread = thread
        thread.start()
        return None

    def _fetch_and_prune(self, host: str, http_port: int, image_key: str,
                          dest: str) -> None:
        if not fetch(host, http_port, image_key, dest,
                     px=NOW_PLAYING_PX, max_bytes=MAX_ART_BYTES):
            return
        _prune(self._dir, keep=dest)
        if self._on_ready:
            try:
                self._on_ready()
            except Exception:
                LOG.exception("art on_ready callback failed")


class RowArtCache:
    """Browse-row thumbnails, fetched by a fixed pool into `rowart/`.

    Same contract as `Cache.get()`: never blocks on the network, returns a
    publishable path or None, and schedules a fetch for a key it does not
    have. What differs is scale -- a browse reply carries up to a page of
    keys at once -- so fetches go through ROW_FETCH_WORKERS daemon threads
    reading a queue bounded by MAX_ROW_PENDING, a key is never queued twice,
    and a key that failed is left alone for ROW_RETRY_AFTER seconds.

    Daemon threads, not a ThreadPoolExecutor: the executor's workers are
    joined at interpreter exit, so a stop with a queue of fetches behind a
    stalled Core would hold the daemon up for FETCH_TIMEOUT per queued key.
    """

    def __init__(self, runtime_dir: str, workers: int = ROW_FETCH_WORKERS) -> None:
        self._dir = os.path.join(runtime_dir, "rowart")
        # Eager for the same reason as Cache: RuntimeDirectory= is wiped on
        # every stop.
        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._pending: dict[str, threading.Event] = {}
        self._failed: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._workers = [threading.Thread(target=self._work, daemon=True,
                                          name="tonearm-rowart-%d" % i)
                         for i in range(workers)]
        for thread in self._workers:
            thread.start()

    def lookup(self, image_key: str | None) -> str | None:
        """The cached path for `image_key` if it is publishable, else None.

        Touches the file's mtime on a hit, so _prune's oldest-first is
        least-recently-USED and a page being looked at keeps its art.
        """
        if not image_key:
            return None
        dest = path_for(self._dir, image_key)
        if not is_publishable(dest, max_bytes=MAX_ROW_ART_BYTES):
            return None
        try:
            os.utime(dest)
        except OSError:
            pass
        return dest

    def get(self, host: str | None, http_port: int | None,
            image_key: str | None) -> str | None:
        if not image_key or not host:
            return None
        path = self.lookup(image_key)
        if path is not None:
            return path
        with self._lock:
            if image_key in self._pending:
                return None
            failed_at = self._failed.get(image_key)
            if failed_at is not None:
                if time.monotonic() - failed_at < ROW_RETRY_AFTER:
                    return None
                del self._failed[image_key]
            if len(self._pending) >= MAX_ROW_PENDING:
                return None
            done = threading.Event()
            self._pending[image_key] = done
        self._queue.put((host, http_port or 9330, image_key, done))
        return None

    def wait(self, keys, timeout: float) -> None:
        """Block until every scheduled fetch among `keys` has finished, or
        `timeout` seconds have passed in total. Keys not in flight are
        skipped, so this never waits on something that will not happen."""
        deadline = time.monotonic() + timeout
        for key in keys:
            with self._lock:
                done = self._pending.get(key)
            if done is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            done.wait(remaining)

    def close(self) -> None:
        for _ in self._workers:
            self._queue.put(None)
        for thread in self._workers:
            thread.join(FETCH_TIMEOUT + 1)

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            host, http_port, image_key, done = job
            ok = False
            try:
                dest = path_for(self._dir, image_key)
                ok = fetch(host, http_port, image_key, dest,
                           px=ROW_PX, max_bytes=MAX_ROW_ART_BYTES)
                if ok:
                    _prune(self._dir, keep=dest, max_files=MAX_ROW_CACHED)
            except Exception:
                # fetch() never raises by contract; this keeps a worker alive
                # if that contract is ever broken, since a dead worker is a
                # silently shrinking pool.
                LOG.exception("row art fetch for %s failed", image_key)
            finally:
                with self._lock:
                    self._pending.pop(image_key, None)
                    if not ok:
                        self._failed[image_key] = time.monotonic()
                        self._failed.move_to_end(image_key)
                        while len(self._failed) > MAX_ROW_FAILED:
                            self._failed.popitem(last=False)
                done.set()


class CachingSession:
    """Wraps a session (anything with `.snapshot()` / `.command()` /
    `.browse()`, i.e. a `core.RoonSession`) to attach a locally-cached
    `art_path` to the followed zone's `now_playing`, beside `image_key`.

    Every consumer of `.snapshot()` -- the immediate reply to a new
    subscriber and every broadcast alike -- goes through this one method,
    so both see the same art_path logic without server.py needing to know
    art exists at all.

    Before this wrapper existed, `RoonSession.snapshot()` (and the
    `_zones()` -> `Arbiter.observe()`/`select()` call inside it) was only
    ever entered from one thread: the Roon event callback, serially, via
    `_publish()`. tonearmd's wiring changes that -- every `subscribe`
    spawns a `server.py` handler thread that calls `.snapshot()`, and a
    completed background art fetch triggers another one via `on_ready` --
    so `Arbiter`'s unlocked `_last_state`/`_started_at`/`_counter` can now
    be mutated by more than one thread at once. `snapshot()` below
    serializes every call that reaches `RoonSession` *through this
    wrapper*, which is every call tonearmd's own wiring makes.

    What this does NOT cover: `RoonSession._publish()` calls `self.snapshot()`
    directly, inside core.py, before it ever reaches `on_change` -- that call
    bypasses this wrapper entirely. `core.py`'s own `_raw_zones()` defends the
    one genuinely racy part of that (a concurrent mutation of
    `self._api.zones` escaping as `RuntimeError` -- see its docstring); this
    lock is not what closes that, and does not need to be, since that call
    remains exactly as serial with respect to ITSELF as it was before this
    wrapper existed (Roon's own callback thread processes events one at a
    time). It is simply a second, independent caller of `RoonSession`,
    outside anything `CachingSession` can reach.

    Also not covered: `command()` is deliberately left unlocked here, even
    though `RoonSession.command()` can itself call `_zones()`. This is NOT
    to avoid a two-thread lock-ordering cycle -- tracing it shows there
    isn't one to avoid. It is to avoid a same-thread REENTRANT deadlock: if
    `command()` took this lock too, a "zone" pin/unpin would walk, on ONE
    thread, still holding this (non-reentrant) lock, through
    `RoonSession.command()` -> `_command_locked` -> `_publish()` ->
    `on_change` -> `broadcast_current()` -> `session.snapshot()` --
    which is `CachingSession.snapshot()` again, trying to reacquire the
    SAME lock the SAME thread already holds. That deadlocks immediately and
    deterministically on the very first zone pin or unpin, with no second
    thread and no race required -- the thread never gets anywhere near
    `srv.broadcast()`/`Server._lock` at all, since it never gets past
    reacquiring its own lock. `RoonSession.command()` already serializes
    against itself via its own internal lock; a command racing a
    `snapshot()` read is a narrower, pre-existing-shaped gap than the
    multi-subscriber hazard this class exists to close.

    As shipped, this has no live deadlock against `server.py`'s
    `Server._lock` either: the only path that holds both is the subscribe
    handshake, where `Server._lock` is always the OUTER lock (acquired
    first, in `_handle()`) and this class's lock is acquired and released
    strictly inside it, briefly, while `snapshot()` runs. Every other path
    that reaches both (`on_change` / `broadcast_current()`) computes
    `session.snapshot()` as a plain value FIRST -- releasing this lock
    completely -- before ever calling `srv.broadcast(...)`, which is the
    only thing that needs `Server._lock`. So the two locks are never
    nested in the opposite order anywhere in the current code. THIS is the
    property that must not be broken by a future change (e.g. broadcasting
    while still "inside" a `snapshot()` call, or locking `command()`
    after all) -- if it ever is, re-derive the lock-ordering argument from
    scratch rather than assuming the old one still holds.
    """

    def __init__(self, session, cache: Cache,
                 row_cache: RowArtCache | None = None) -> None:
        self._session = session
        self._cache = cache
        self._row_cache = row_cache
        self._lock = threading.Lock()
        # (host, http_port) of the Core the last snapshot reported, for row
        # art: a browse reply does not carry it. One tuple assignment, so a
        # browse thread reading it never sees half of one.
        self._core: tuple | None = None

    def snapshot(self) -> dict:
        with self._lock:
            payload = self._session.snapshot()
            zone = payload.get("zone")
            core = payload.get("core")
            self._core = ((core.get("host"), core.get("http_port"))
                          if core and core.get("host") else None)
            if zone and core:
                np = zone.get("now_playing")
                if np is not None:
                    np["art_path"] = self._cache.get(
                        core.get("host"), core.get("http_port"), np.get("image_key"))
            return payload

    def command(self, verb: str, arg=None) -> None:
        self._session.command(verb, arg)

    def player_targeted(self, zone_id, verb, arg=None, output_id=None,
                        output_ids=None) -> dict:
        # Forwarded, and deliberately NOT under self._lock, for the same
        # reasons `command()` is: it is a round-trip to the Core, and this
        # lock is non-reentrant.
        #
        # The daemon wraps the session in this class BEFORE handing it to the
        # server (scripts/tonearmd), so a route that is not forwarded here
        # is not reachable at all: the server would answer "unsupported" for
        # every request. The wrapper forwards `command()` and `browse()` for
        # exactly this reason, and the player route is the third method the
        # server drives.
        return self._session.player_targeted(zone_id, verb, arg, output_id,
                                              output_ids)

    def browse(self, key: str, op: str, **kwargs) -> dict:
        # Deliberately NOT under self._lock, for two independent reasons:
        #
        # 1. A browse is a network round-trip to the Roon Core, far slower
        #    than a snapshot. Holding this lock for its duration would stall
        #    every subscriber's snapshot() call for as long as the browse
        #    took. Keeping browse off the subscriber path is a core design
        #    property of this feature, not an oversight.
        #
        # 2. The same reentrancy hazard documented above for command(): this
        #    lock is non-reentrant, and a locked browse() risks the same
        #    one-thread self-deadlock shape -- some future browse op that
        #    ends up calling back into snapshot() on the same thread would
        #    hang trying to reacquire a lock it already holds.
        #
        # Browse replies carry image_key per row. Until 0.14.2 the widget
        # built a Core URL from it and fetched that inside the shell; now
        # each row gains `art_path` here instead, the same way now_playing
        # does in snapshot(), and the widget only ever loads that file.
        return self._attach_row_art(self._session.browse(key, op, **kwargs))

    def _attach_row_art(self, reply):
        rows = reply.get("rows") if isinstance(reply, dict) else None
        if self._row_cache is None or not isinstance(rows, list) or not rows:
            return reply
        core = self._core
        keys = [row.get("image_key") if isinstance(row, dict) else None
                for row in rows]
        if core is not None:
            host, http_port = core
            # In row order, so the queue serves the visible rows first.
            for image_key in keys:
                if image_key:
                    self._row_cache.get(host, http_port, image_key)
            first = [k for k in keys[:ROW_ART_WAIT_ROWS] if k]
            self._row_cache.wait(first, ROW_ART_WAIT)
        out = []
        for row, image_key in zip(rows, keys):
            if not isinstance(row, dict):
                out.append(row)
                continue
            # A new dict: BrowseSession.current() copies its row LIST, not
            # the rows, so writing into these would edit the session's state.
            row = dict(row)
            row["art_path"] = (self._row_cache.lookup(image_key)
                               if image_key and core is not None else None)
            out.append(row)
        reply = dict(reply)
        reply["rows"] = out
        return reply
