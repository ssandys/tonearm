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

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")


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
    a non-2xx response, or a malformed/truncated body all end in a logged
    warning and `False`, so a bad fetch degrades to `art_path: null` instead
    of taking the daemon down.
    """
    url = _image_url(host, http_port, image_key)
    try:
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            data = response.read()
        tmp = dest + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, dest)  # atomic: a concurrent reader never sees a partial file
        return True
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        # OSError/URLError cover network failures and non-2xx responses;
        # http.client.HTTPException (e.g. IncompleteRead on a truncated
        # body) is a *sibling* of OSError, not a subclass of it, so it needs
        # naming explicitly or it would slip past this handler and crash the
        # fetch thread.
        LOG.warning("art fetch failed for %s", image_key, exc_info=True)
        return False


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
        if os.path.exists(dest):
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
    """Wraps a session (anything with `.snapshot()` / `.command()`, i.e. a
    `core.RoonSession`) to attach a locally-cached `art_path` to the
    followed zone's `now_playing`, beside `image_key`.

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
