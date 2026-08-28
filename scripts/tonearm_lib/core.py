"""The Roon side of tonearmd: connect, pair, subscribe, issue transport.

This module is mostly I/O. Everything decidable without a Core lives in
state.py and zones.py, which is why most of this file has no unit tests --
verify it against yavin. `_seeded_api()` is the one exception: a pure,
Core-independent attribute-isolation helper, and it has a regression test in
tests/python/test_core.py.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor"))

from roonapi import RoonApi              # noqa: E402  (vendored, path-inserted)

from . import config, sood, state, zones  # noqa: E402

LOG = logging.getLogger("tonearmd.core")

APPINFO = {
    "extension_id": "com.onemanposse.tonearm",
    "display_name": "tonearm",
    "display_version": "0.1.0",
    "publisher": "Sean Sandys",
    "email": "sean@onemanposse.com",
}

# Seconds to wait for one port to answer the MOO/WS handshake before giving
# up on it. Measured against yavin (Roon 2.71 build 1683): the SOOD-advertised
# tcp_port (9150) accepts a TCP connection and then never answers the
# WebSocket upgrade -- a silent hang, not an error. RoonApi's own
# blocking_init has no ceiling on that wait, so without a bound here
# RoonApi.__init__ blocks forever and the daemon looks "healthy" while doing
# nothing. See docs/superpowers/specs/2026-08-27-tonearm-design.md S2.1.
CONNECT_TIMEOUT = 10.0

# Seconds to wait, after signalling a timed-out attempt to give up, for it to
# actually finish before moving on to the next candidate port regardless.
#
# This is NOT what makes cross-instance state sharing safe -- that is
# `_seeded_api()`, below, which gives every RoonApi instance its own private
# `_zones`/`_outputs` from the moment it is created, before anything else
# can touch it. That closes the actual correctness risk deterministically,
# with no dependence on timing. See `_seeded_api()`'s docstring.
#
# What STOP_GRACE still does: narrow how long a timed-out attempt's thread
# and open socket can go on existing in the background before `_connect`
# moves to the next port. For an abandoned attempt that eventually reaches
# registration late (a genuinely slow-but-working port, not a dead one),
# this reliably lets it finish and exit -- its zones/outputs prefetch is
# bounded purely by retry count (roonapi.py:983-996, ~2.5s per call,
# independent of `_exit` or socket state) once `ready` is True. Two such
# calls bound to ~5.0s, the same as this constant -- an exact tie, not a
# comfortable margin, once the ready-wait loop's own 50ms poll granularity,
# `send_request()`'s round trip, and scheduling jitter are accounted for.
# That imprecision no longer matters for correctness now that `_seeded_api`
# isolates instance state regardless of timing; it only means a slow port
# occasionally still gets logged as "did not unwind" when it was in fact
# about to.
#
# For an abandoned attempt that never gets past the initial handshake read
# at all -- port 9150's actual measured behavior against yavin (TCP
# connects, then silence, forever) -- this is a THREAD AND SOCKET LEAK, not
# a data risk: `on_open` never fires, so `_socket_connected()` never sends
# `registry/register`, so `_server_registered()` never runs, so `.subscribe`
# is never called and the "zones"/"outputs" item-mutation at roonapi.py:
# 900-903 is never reached for that instance at all -- there is nothing left
# for it to corrupt. Measured directly: a local test server reproducing that
# exact behavior (accept the connection, send nothing) left the abandoned
# thread still blocked in that read even after stop()'s
# self._socket.close() and this full grace period elapsed -- it only
# unblocked once the test closed the server socket and a RST arrived.
# Nothing on our side can manufacture that RST; only the remote Core (or a
# firewall) can, and by definition it never does for this specific failure.
# No finite STOP_GRACE closes this leak, so on timeout this code proceeds to
# the next port anyway (logged loudly) rather than letting one permanently
# dead port block discovery of a working one forever. In practice this leak
# is rarely exercised at all: `_candidate_ports()` tries `http_port` first,
# and `http_port` has answered reliably every time it has been observed --
# `tcp_port` is the one that leaks, and it is only reached as a fallback.
STOP_GRACE = 5.0


def _seeded_api() -> RoonApi:
    """A bare RoonApi instance with its own private `_zones`/`_outputs`.

    `_zones` and `_outputs` are mutable CLASS-level defaults in the vendored
    library (roonapi.py:52-53), not instance attributes. Until an instance
    reassigns them itself -- which RoonApi.__init__ only does once `ready`
    is confirmed, or not at all if it never gets that far -- any write to
    `self._zones[...]` from anywhere lands in the ONE dict every instance
    shares. With a single connection attempt per process that was invisible;
    with port fallback, a timed-out-and-abandoned attempt and a fresh
    attempt for the next port can briefly coexist, and if the abandoned one
    ever reaches registration late, its subscription could write into the
    dict the new instance is also relying on.

    Assigning fresh instance dicts here -- via `RoonApi.__new__`, before
    `__init__` (and therefore `_server_setup()`, which starts the
    background socket thread) ever runs -- closes that deterministically
    rather than merely narrowing the window: at the moment these lines run,
    the instance exists but nothing else does yet, so there is no other
    thread that could be racing this assignment. Every instance this
    function returns, whether it goes on to win or get abandoned, is
    isolated from the shared class-level dict for its entire life. See
    STOP_GRACE's comment for what is left for it to still guard (thread and
    socket cleanup, not correctness) once instances no longer share state.
    """
    api = RoonApi.__new__(RoonApi)
    api._zones = {}
    api._outputs = {}
    return api


class RoonSession:
    """Owns the Roon connection and publishes normalized state on change."""

    def __init__(self, on_change) -> None:
        self._on_change = on_change
        self._api: RoonApi | None = None
        self._cfg = config.load()
        self._arbiter = zones.Arbiter(self._cfg.get("pinned_zone_id"))
        self._status = "connecting"
        self._lock = threading.Lock()

    @property
    def status(self) -> str:
        return self._status

    def snapshot(self) -> dict:
        core = None
        if self._cfg.get("host"):
            core = {"host": self._cfg["host"],
                    "http_port": self._cfg.get("http_port", 9330),
                    "name": self._cfg.get("name") or self._cfg["host"]}
        listing, selected = self._zones()
        return state.build(self._status, core, selected, listing)

    def _zones(self):
        if not self._api:
            return [], None
        raw = list((self._api.zones or {}).values())
        listing = [z for z in (state.normalize_zone(r) for r in raw) if z]
        self._arbiter.observe(listing)
        return listing, self._arbiter.select(listing)

    def _publish(self, *_args) -> None:
        try:
            self._on_change(self.snapshot())
        except Exception:
            LOG.exception("publish failed")

    def start(self) -> None:
        if not self._cfg.get("host"):
            cores = sood.discover()
            if not cores:
                self._status = "unreachable"
                self._publish()
                return
            core = cores[0]
            LOG.info("discovered %s at %s via %s",
                     core["name"], core["host"], core["via"])
            self._cfg.update({k: core[k] for k in ("host", "tcp_port", "http_port", "name")})
            config.save(self._cfg)

        token = config.load_token()
        if token is None:
            # First run: RoonApi blocks until the extension is enabled by hand
            # in Roon Remote -> Settings -> Extensions.
            self._status = "unpaired"
            self._publish()

        self._api = self._connect(token)
        if self._api is None:
            self._status = "unreachable"
            self._publish()
            return

        if self._api.token:
            config.save_token(self._api.token)

        self._status = "ok"
        self._api.register_state_callback(self._publish)
        self._publish()

    def _candidate_ports(self) -> list[int]:
        """MOO/WS ports to try, in order.

        SOOD advertises `tcp_port`, but on Roon 2.71 that port accepts the
        TCP connection and then never answers the WebSocket handshake -- MOO
        actually lives on `http_port` on this Core. Try `http_port` first,
        falling back to `tcp_port` for an older Core where the advertised
        port really is the live one. Never hardcode or swap the two: both
        stay in config exactly as discovered/reported.
        """
        http_port = self._cfg.get("http_port") or 9330
        tcp_port = self._cfg.get("tcp_port") or 9150
        ports = [http_port]
        if tcp_port != http_port:
            ports.append(tcp_port)
        return ports

    def _connect(self, token) -> RoonApi | None:
        """Try each candidate port in turn, each bounded by CONNECT_TIMEOUT.
        Returns a ready RoonApi, or None if nothing answered on any port.

        Ports are tried strictly one at a time. `_try_port` waits (bounded
        by STOP_GRACE) for a timed-out attempt to unwind before returning,
        so a second RoonApi instance is rarely even alive at the same time
        as the first -- but that is no longer what keeps this safe. Every
        instance `_try_port` creates gets its own private `_zones`/
        `_outputs` from `_seeded_api()` before anything else can touch them,
        so even on the rare occasion two do overlap, neither can corrupt the
        other's state. See `_seeded_api()`'s and STOP_GRACE's comments.
        """
        host = self._cfg["host"]
        ports = self._candidate_ports()
        for port in ports:
            api = self._try_port(host, port, token)
            if api is not None:
                return api
        LOG.error("no MOO response from %s on any of %s (%.0fs each)",
                  host, ports, CONNECT_TIMEOUT)
        return None

    @staticmethod
    def _try_port(host: str, port: int, token) -> RoonApi | None:
        """Connect on one port, bounded by CONNECT_TIMEOUT.

        This deliberately does NOT use RoonApi(..., blocking_init=False) to
        get a bound. That looked like the obvious approach, but when a token
        is already known it backfires: blocking_init only skips the
        ready-wait loop, not the unconditional `if self.token: self._zones =
        self._get_zones()` step right after it -- and that step fires before
        the socket has even connected. It always loses that race, times out
        after its own ~2.5s retry budget, and then *overwrites* self._zones
        with {} -- discarding the real zone list the async "zones"
        subscription delivers correctly a few milliseconds after
        registration (self._zones is also a mutable class-level default,
        which is how the subscription callback can populate it before the
        instance even has its own copy). Confirmed against yavin by
        instrumenting _on_state_change: the two real zones land, then get
        clobbered by the trailing empty-dict assignment. Zones would then
        stay empty forever for a zone that never changes state again.

        blocking_init=True does not have that race -- its zones/outputs
        prefetch only runs once `ready` is confirmed, so it reliably
        succeeds. Its own wait loop is unbounded, though (nothing ever sets
        `ready` or `_exit` if the port never answers), which is the original
        problem. So: keep blocking_init=True's safe ordering, but run it in
        our own thread and bound it with an external join timeout. __new__
        is called separately from __init__ (via `_seeded_api()`) so there is
        a handle to `api` to call .stop() on even if __init__ itself never
        returns -- and so `_zones`/`_outputs` can be seeded as private
        instance dicts before __init__ starts the background socket thread
        that could otherwise race that assignment. See `_seeded_api()`.

        A timed-out attempt is also joined again (bounded by STOP_GRACE)
        after stop() before this returns. That is not what prevents state
        corruption -- `_seeded_api()` already guarantees this instance can
        never mutate another's `_zones`/`_outputs`, regardless of timing --
        it just narrows how long the abandoned attempt's thread and open
        socket can go on existing before `_connect` moves to the next port.
        """
        api = _seeded_api()
        errors: list[Exception] = []

        def init() -> None:
            try:
                RoonApi.__init__(api, APPINFO, token, host, port, blocking_init=True)
            except Exception as exc:  # reported via `errors`, not swallowed
                errors.append(exc)

        thread = threading.Thread(target=init, daemon=True)
        thread.start()
        thread.join(CONNECT_TIMEOUT)

        if thread.is_alive():
            # Silent hang: TCP connected but the WebSocket handshake never
            # answered -- exactly what the SOOD-advertised tcp_port does on
            # this Roon version. Signal the stuck init to give up, then wait
            # (bounded) to actually confirm it has, before returning -- this
            # is thread/socket-leak hygiene, not a correctness guard (that is
            # `_seeded_api()`'s job). See STOP_GRACE's comment for what this
            # join does and does not clean up.
            LOG.warning("no response from %s:%s within %.0fs", host, port, CONNECT_TIMEOUT)
            try:
                api.stop()
            except Exception:
                LOG.exception("error tearing down timed-out connection to %s:%s", host, port)
            thread.join(STOP_GRACE)
            if thread.is_alive():
                LOG.error(
                    "connection attempt to %s:%s did not unwind within %.0fs of "
                    "stop(); abandoning it and moving on. It may still be running "
                    "and could briefly overlap with the next connection attempt.",
                    host, port, STOP_GRACE)
            return None

        if errors:
            LOG.exception("connect to %s:%s failed", host, port, exc_info=errors[0])
            return None

        LOG.info("connected to %s:%s", host, port)
        return api

    def stop(self) -> None:
        if self._api:
            self._api.stop()
            self._api = None

    # -- commands -------------------------------------------------------
    def command(self, verb: str, arg=None) -> None:
        with self._lock:
            self._command_locked(verb, arg)

    def _command_locked(self, verb: str, arg) -> None:
        if verb == "zone":
            if arg == "unpin":
                self._arbiter.unpin()
                self._cfg["pinned_zone_id"] = None
            else:
                self._arbiter.pin(arg)
                self._cfg["pinned_zone_id"] = arg
            config.save(self._cfg)
            self._publish()
            return

        if not self._api:
            LOG.warning("dropping %s: not connected", verb)
            return
        _, selected = self._zones()
        if not selected:
            LOG.warning("dropping %s: no followed zone", verb)
            return
        zid = selected["id"]

        if verb in ("playpause", "play", "pause", "next", "previous"):
            self._api.playback_control(zid, verb)
        elif verb == "seek":
            self._api.seek(zid, int(arg), "absolute")
        elif verb == "volume":
            # state.py exposes the output's raw min/max/value scale (not a
            # 0-100 percent), so the raw setter -- not change_volume_percent
            # -- is the one that matches what the widget's slider shows.
            for output in (self._api.zones.get(zid, {}).get("outputs") or []):
                self._api.change_volume_raw(output["output_id"], int(arg), "absolute")
        elif verb in ("mute", "unmute"):
            for output in (self._api.zones.get(zid, {}).get("outputs") or []):
                self._api.mute(output["output_id"], verb == "mute")
        else:
            LOG.warning("unknown verb %r", verb)
