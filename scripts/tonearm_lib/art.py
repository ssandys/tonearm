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

import logging
import os
import re
import threading
import urllib.error
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


def fetch(host: str, http_port: int, image_key: str, dest: str) -> bool:
    """Fetch a 64px copy of `image_key`'s art into `dest`.

    Returns whether it succeeded. Never raises: a network error, a timeout,
    or a non-2xx response all end in a logged warning and `False`, so a bad
    fetch degrades to `art_path: null` instead of taking the daemon down.
    """
    url = "http://%s:%d/api/image/%s?scale=fit&width=64&height=64" % (
        host, http_port, image_key)
    try:
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            data = response.read()
        tmp = dest + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, dest)  # atomic: a concurrent reader never sees a partial file
        return True
    except (OSError, urllib.error.URLError, ValueError):
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
    """

    def __init__(self, session, cache: Cache) -> None:
        self._session = session
        self._cache = cache

    def snapshot(self) -> dict:
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
