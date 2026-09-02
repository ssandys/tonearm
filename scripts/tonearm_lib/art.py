"""Local cache of a small cover-art copy, for `ColorQuantizer`.

Measured in a live Quickshell probe: `ColorQuantizer` pointed at the Core's
remote `http://<core>:9330/api/image/<key>` URL emits **zero** colors;
pointed at the identical bytes as a local `file://` it emits eight. A plain
`Image` loads the remote URL fine, so this only affects accent extraction,
not the art display. Left unfixed, the album-art accent would silently do
nothing -- always falling back to the theme accent, with no error anywhere.

So the daemon keeps a local copy: when the followed zone's `image_key`
changes, fetch a 64px copy (what the quantizer samples) into
`$XDG_RUNTIME_DIR/tonearm/art/<image_key>.jpg` and hand back its path.

This module owns I/O only. It never decides how art is displayed -- it
emits a path, or `None`, and nothing else. Fetching happens on a background
thread so it can never block a state broadcast; a failed fetch is logged
and reported as `None`, never raised.
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import secrets
import stat
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("tonearmd.art")

# Seconds to wait for the Core to answer one image request. This is a 64px
# thumbnail on the LAN; a Core that cannot answer this quickly is not one
# whose art is worth waiting on -- give up and try again next track.
FETCH_TIMEOUT = 5.0

# How many cached images to keep on disk. One image_key per track and only
# the followed zone is ever cached, so this is generous headroom, not a
# tight budget -- just enough to stop the directory from growing without
# bound over a long-running daemon.
MAX_CACHED = 10

# The URL below asks the Core for a 64x64 thumbnail, which measures a few KB.
# Only the reader can enforce that it got one: the request is a preference, and
# whatever answers on that host:port decides what to send. 1 MiB is ~100x the
# real size and still far too small to matter to the daemon.
#
# The same number bounds what is PUBLISHED as `art_path`, because that file is
# read by ColorQuantizer inside the shared shell process, and Quickshell
# exports no filesystem primitive able to bound it there.
MAX_ART_BYTES = 1024 * 1024

# Bounds on what the bytes DECODE to, which the byte cap above cannot see. A
# few hundred bytes of PNG header can declare 30000x30000 -- 3.6 GB of RGBA in
# whatever opens it, which here is ColorQuantizer inside the shared shell
# process. The URL asks for 64x64; 2048 per side is a wide margin for a Core
# that ignores the hint and returns a full-size cover, and still two orders of
# magnitude below a bomb.
MAX_ART_DIMENSION = 2048
MAX_ART_PIXELS = MAX_ART_DIMENSION * MAX_ART_DIMENSION

# How much of a cached file is read back to re-check its header. The daemon
# wrote it, so the header is at the front; this is a page-cache read of at
# most 64 KiB, once per snapshot.
HEADER_WINDOW = 64 * 1024

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")

# Start-of-frame markers, which carry the dimensions. C4 (Huffman table), C8
# (JPEG extensions) and CC (arithmetic coding conditioning) share the range
# but are not frames.
_SOF_MARKERS = frozenset(m for m in range(0xC0, 0xD0)
                         if m not in (0xC4, 0xC8, 0xCC))
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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


def _image_url(host: str, http_port: int, image_key: str) -> str:
    # image_key is untrusted (it comes from the Core); percent-encode it
    # rather than interpolating it raw, the same caution _safe_name already
    # applies on the filesystem side.
    return "http://%s:%d/api/image/%s?scale=fit&width=64&height=64" % (
        host, http_port, urllib.parse.quote(image_key, safe=""))


def fetch(host: str, http_port: int, image_key: str, dest: str) -> bool:
    """Fetch a 64px copy of `image_key`'s art into `dest`.

    Returns whether it succeeded. Never raises: a network error, a timeout,
    a non-2xx response, a redirect off the Core, a malformed/truncated body
    or a body that is not a small image all end in a logged warning and
    `False`, so a bad fetch degrades to `art_path: null` instead of taking
    the daemon down.
    """
    url = _image_url(host, http_port, image_key)
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
            data = response.read(MAX_ART_BYTES + 1)
        if len(data) > MAX_ART_BYTES:
            LOG.warning("art for %s exceeds %d bytes; refusing it",
                        image_key, MAX_ART_BYTES)
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


def is_publishable(path: str) -> bool:
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

    The header is re-read rather than trusted from fetch time, because the
    threat is a file swapped after it was written.

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
        if info.st_size > MAX_ART_BYTES:
            return False
        with os.fdopen(fd, "rb") as handle:
            fd = None                       # the file object owns it now
            header = handle.read(HEADER_WINDOW)
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    dims = image_dimensions(header)
    return dims is not None and _within_bounds(dims)


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
    candidates = [n for n in names if n != keep_name and not n.endswith(".tmp")]
    over = len(candidates) - (max_files - 1)
    if over <= 0:
        return
    candidates.sort(key=lambda n: os.path.getmtime(os.path.join(directory, n)))
    for name in candidates[:over]:
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
        if not fetch(host, http_port, image_key, dest):
            return
        _prune(self._dir, keep=dest)
        if self._on_ready:
            try:
                self._on_ready()
            except Exception:
                LOG.exception("art on_ready callback failed")


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

    def __init__(self, session, cache: Cache) -> None:
        self._session = session
        self._cache = cache
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            payload = self._session.snapshot()
            zone = payload.get("zone")
            core = payload.get("core")
            if zone and core:
                np = zone.get("now_playing")
                if np is not None:
                    np["art_path"] = self._cache.get(
                        core.get("host"), core.get("http_port"), np.get("image_key"))
            return payload

    def command(self, verb: str, arg=None) -> None:
        self._session.command(verb, arg)

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
        # Pure pass-through, no art logic: browse replies carry image_key,
        # and it's the widget's job to build URLs from it, not this class's.
        return self._session.browse(key, op, **kwargs)
